"""Database-owned Composer lifecycle counts and latest-generation progress.

Every operation serializes on the current identity and session ownership rows.
Request tokens count queued work independently of the latest publisher's custody.
No database error is converted into process-local progress.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, Row, Select, delete, func, insert, select, update

from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.progress import ComposerProgressSnapshot, ComposerRequestLease
from elspeth.web.coordination.membership_authority import _database_clock_value, _ensure_utc, _require_nonblank
from elspeth.web.sessions.models import (
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    identities_table,
    sessions_table,
)

_MAX_SNAPSHOT_BYTES = 16384


def _ownership_query(session_id: str, user_id: str, *, allow_archived: bool = False) -> Select[Any]:
    query = select(sessions_table.c.id).where(sessions_table.c.id == session_id, sessions_table.c.user_id == user_id)
    if not allow_archived:
        query = query.where(sessions_table.c.archived_at.is_(None))
    return query.with_for_update()


def _identity_query(user_id: str) -> Select[Any]:
    return (
        select(identities_table.c.identity_id)
        .where(identities_table.c.identity_id == user_id, identities_table.c.access_state == "active")
        .with_for_update()
    )


def _snapshot(session_id: str, request_id: str | None, event: ComposerProgressEvent, now: datetime) -> ComposerProgressSnapshot:
    return ComposerProgressSnapshot(
        session_id=session_id,
        request_id=request_id,
        updated_at=now,
        phase=event.phase,
        headline=event.headline,
        evidence=event.evidence,
        likely_next=event.likely_next,
        reason=event.reason,
    )


def _encode(snapshot: ComposerProgressSnapshot) -> str:
    encoded = snapshot.model_dump_json()
    if len(encoded.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("Composer progress snapshot exceeds persistence limit")
    return encoded


def _read_snapshot(row: Row[Any] | None, session_id: str, user_id: str, now: datetime, count: int) -> ComposerProgressSnapshot:
    if type(count) is not int or count < 0:
        raise RuntimeError("Composer progress inflight count is corrupt")
    if row is not None:
        if row.identity_id != user_id or row.session_id != session_id:
            raise RuntimeError("Composer progress snapshot ownership is corrupt")
        if type(row.generation) is not str or not row.generation.strip():
            raise RuntimeError("Composer progress snapshot generation is corrupt")
        if row.request_token is not None and (type(row.request_token) is not str or not row.request_token.strip()):
            raise RuntimeError("Composer progress snapshot request token is corrupt")
        if row.snapshot_json is None and row.request_token is None:
            raise RuntimeError("Composer progress snapshot has neither a publisher nor a replay")
        if _ensure_utc(row.expires_at) <= _ensure_utc(row.updated_at):
            raise RuntimeError("Composer progress snapshot expiry is corrupt")
    if row is not None and row.snapshot_json is not None:
        if len(row.snapshot_json.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise RuntimeError("Composer progress snapshot exceeds persistence limit")
        snapshot = ComposerProgressSnapshot.model_validate_json(row.snapshot_json)
        if snapshot.model_dump_json() != row.snapshot_json:
            raise RuntimeError("Composer progress snapshot is not canonical")
        if snapshot.inflight_requests != 0:
            raise RuntimeError("Composer progress snapshot contains a persisted live count")
        if snapshot.session_id != session_id or snapshot.request_id != row.request_id:
            raise RuntimeError("Composer progress snapshot identity is corrupt")
        if snapshot.updated_at != _ensure_utc(row.updated_at):
            raise RuntimeError("Composer progress snapshot timestamp is corrupt")
        if _ensure_utc(row.expires_at) > now:
            return snapshot.model_copy(update={"inflight_requests": count})
    return ComposerProgressSnapshot(
        session_id=session_id,
        request_id=None,
        updated_at=now,
        phase="starting" if count else "idle",
        headline="Composer work is queued or starting." if count else "No active composer work.",
        reason=None if count else "composer_idle",
        inflight_requests=count,
    )


class ComposerRequestLeaseLost(RuntimeError):
    """A request's exact lifecycle lease has expired or disappeared."""


class SessionComposerProgressAuthority:
    """Sole writer of the durable Composer progress and inflight tables."""

    def __init__(self, engine: Engine, *, owner_instance_id: str, lease_seconds: int = 60, snapshot_ttl_seconds: int = 86400) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Durable Composer progress requires PostgreSQL")
        _require_nonblank(owner_instance_id, "owner_instance_id")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer between 1 and 3600")
        if type(snapshot_ttl_seconds) is not int or snapshot_ttl_seconds < lease_seconds:
            raise ValueError("snapshot TTL must cover at least one request lease")
        self._engine = engine
        self._owner_instance_id = owner_instance_id
        self._lease_seconds = lease_seconds
        self._snapshot_ttl_seconds = snapshot_ttl_seconds

    def begin_request(self, session_id: str, user_id: str) -> ComposerRequestLease:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(user_id, "user_id")
        self.cleanup_expired()
        lease = ComposerRequestLease(request_token=uuid4().hex, session_id=session_id, user_id=user_id)
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(session_id, user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            conn.execute(
                insert(composer_inflight_requests_table).values(
                    request_token=lease.request_token,
                    session_id=session_id,
                    identity_id=user_id,
                    owner_instance_id=self._owner_instance_id,
                    begun_at=now,
                    expires_at=now + timedelta(seconds=self._lease_seconds),
                )
            )
        return lease

    def cleanup_expired(self, *, limit: int = 100) -> int:
        """Remove bounded abandoned state without waiting on active sessions.

        Candidate discovery is only a hint. Each independent transaction locks
        identity then session, samples database time, and rechecks expiry.
        Snapshot custody survives while any request in its session is live.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cleanup limit must be an integer between 1 and 1000")
        with self._engine.begin() as conn:
            scan_time = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            candidates = conn.execute(
                select(sessions_table.c.id, sessions_table.c.user_id)
                .join(composer_inflight_requests_table, composer_inflight_requests_table.c.session_id == sessions_table.c.id)
                .where(composer_inflight_requests_table.c.expires_at <= scan_time)
                .union(
                    select(sessions_table.c.id, sessions_table.c.user_id)
                    .join(composer_progress_snapshots_table, composer_progress_snapshots_table.c.session_id == sessions_table.c.id)
                    .where(composer_progress_snapshots_table.c.expires_at <= scan_time)
                )
                .order_by("user_id", "id")
                .limit(limit)
            ).all()
        removed = 0
        for candidate in candidates:
            if removed == limit:
                break
            with self._engine.begin() as conn:
                identity = conn.execute(
                    select(identities_table.c.identity_id)
                    .where(identities_table.c.identity_id == candidate.user_id)
                    .with_for_update(skip_locked=True)
                ).one_or_none()
                if identity is None:
                    continue
                session = conn.execute(
                    select(sessions_table.c.id)
                    .where(sessions_table.c.id == candidate.id, sessions_table.c.user_id == candidate.user_id)
                    .with_for_update(skip_locked=True)
                ).one_or_none()
                if session is None:
                    continue
                now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
                tokens = (
                    conn.execute(
                        select(composer_inflight_requests_table.c.request_token)
                        .where(
                            composer_inflight_requests_table.c.session_id == candidate.id,
                            composer_inflight_requests_table.c.expires_at <= now,
                        )
                        .order_by(composer_inflight_requests_table.c.request_token)
                        .limit(limit - removed)
                        .with_for_update(skip_locked=True)
                    )
                    .scalars()
                    .all()
                )
                if tokens:
                    deleted = conn.execute(
                        delete(composer_inflight_requests_table)
                        .where(
                            composer_inflight_requests_table.c.request_token.in_(tokens),
                            composer_inflight_requests_table.c.expires_at <= now,
                        )
                        .returning(composer_inflight_requests_table.c.request_token)
                    ).all()
                    removed += len(deleted)
                if removed == limit:
                    continue
                live_request = (
                    select(composer_inflight_requests_table.c.request_token)
                    .where(
                        composer_inflight_requests_table.c.session_id == candidate.id,
                        composer_inflight_requests_table.c.expires_at > now,
                    )
                    .exists()
                )
                snapshot = conn.execute(
                    select(composer_progress_snapshots_table.c.session_id)
                    .where(
                        composer_progress_snapshots_table.c.session_id == candidate.id,
                        composer_progress_snapshots_table.c.expires_at <= now,
                        ~live_request,
                    )
                    .with_for_update(skip_locked=True)
                ).one_or_none()
                if snapshot is not None:
                    deleted_snapshots = conn.execute(
                        delete(composer_progress_snapshots_table)
                        .where(
                            composer_progress_snapshots_table.c.session_id == candidate.id,
                            composer_progress_snapshots_table.c.expires_at <= now,
                        )
                        .returning(composer_progress_snapshots_table.c.session_id)
                    ).all()
                    removed += len(deleted_snapshots)
        return removed

    def heartbeat_request(self, lease: ComposerRequestLease) -> None:
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(lease.session_id, lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            renewed = conn.execute(
                update(composer_inflight_requests_table)
                .where(
                    composer_inflight_requests_table.c.request_token == lease.request_token,
                    composer_inflight_requests_table.c.session_id == lease.session_id,
                    composer_inflight_requests_table.c.identity_id == lease.user_id,
                    composer_inflight_requests_table.c.owner_instance_id == self._owner_instance_id,
                    composer_inflight_requests_table.c.expires_at > now,
                )
                .values(expires_at=now + timedelta(seconds=self._lease_seconds))
                .returning(composer_inflight_requests_table.c.request_token)
            ).one_or_none()
            if renewed is None:
                raise ComposerRequestLeaseLost("Composer request lease cannot be renewed")

    def end_request(self, lease: ComposerRequestLease) -> None:
        # Teardown remains possible after access revocation or session archival.
        with self._engine.begin() as conn:
            conn.execute(_ownership_query(lease.session_id, lease.user_id, allow_archived=True)).one_or_none()
            conn.execute(
                delete(composer_inflight_requests_table).where(
                    composer_inflight_requests_table.c.request_token == lease.request_token,
                    composer_inflight_requests_table.c.session_id == lease.session_id,
                    composer_inflight_requests_table.c.identity_id == lease.user_id,
                    composer_inflight_requests_table.c.owner_instance_id == self._owner_instance_id,
                )
            )

    def bind_request(self, lease: ComposerRequestLease, request_id: str | None) -> str:
        generation = uuid4().hex
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(lease.session_id, lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            active = conn.execute(
                select(composer_inflight_requests_table.c.request_token).where(
                    composer_inflight_requests_table.c.request_token == lease.request_token,
                    composer_inflight_requests_table.c.session_id == lease.session_id,
                    composer_inflight_requests_table.c.identity_id == lease.user_id,
                    composer_inflight_requests_table.c.owner_instance_id == self._owner_instance_id,
                    composer_inflight_requests_table.c.expires_at > now,
                )
            ).one_or_none()
            if active is None:
                raise ComposerRequestLeaseLost("Composer request cannot claim progress")
            conn.execute(
                delete(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == lease.session_id)
            )
            conn.execute(
                insert(composer_progress_snapshots_table).values(
                    session_id=lease.session_id,
                    identity_id=lease.user_id,
                    generation=generation,
                    request_token=lease.request_token,
                    request_id=request_id,
                    snapshot_json=None,
                    updated_at=now,
                    expires_at=now + timedelta(seconds=self._snapshot_ttl_seconds),
                )
            )
        return generation

    def publish(self, lease: ComposerRequestLease, generation: str, request_id: str | None, event: ComposerProgressEvent) -> None:
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(lease.session_id, lease.user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            active = conn.execute(
                select(composer_inflight_requests_table.c.request_token).where(
                    composer_inflight_requests_table.c.request_token == lease.request_token,
                    composer_inflight_requests_table.c.session_id == lease.session_id,
                    composer_inflight_requests_table.c.identity_id == lease.user_id,
                    composer_inflight_requests_table.c.owner_instance_id == self._owner_instance_id,
                    composer_inflight_requests_table.c.expires_at > now,
                )
            ).one_or_none()
            if active is None:
                raise ComposerRequestLeaseLost("Composer request cannot publish progress")
            snapshot = _snapshot(lease.session_id, request_id, event, now)
            conn.execute(
                update(composer_progress_snapshots_table)
                .where(
                    composer_progress_snapshots_table.c.session_id == lease.session_id,
                    composer_progress_snapshots_table.c.identity_id == lease.user_id,
                    composer_progress_snapshots_table.c.generation == generation,
                    composer_progress_snapshots_table.c.request_token == lease.request_token,
                )
                .values(snapshot_json=_encode(snapshot), updated_at=now, expires_at=now + timedelta(seconds=self._snapshot_ttl_seconds))
            )

    def replay(
        self, session_id: str, user_id: str, request_id: str, event: ComposerProgressEvent, *, lease: ComposerRequestLease | None = None
    ) -> ComposerProgressSnapshot | None:
        if lease is not None and (lease.session_id != session_id or lease.user_id != user_id):
            raise ValueError("Composer replay lease does not match the request")
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(session_id, user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            existing = conn.execute(
                select(composer_progress_snapshots_table.c.session_id).where(
                    composer_progress_snapshots_table.c.session_id == session_id,
                    composer_progress_snapshots_table.c.expires_at > now,
                )
            ).one_or_none()
            if existing is not None:
                return None
            active_tokens = conn.execute(
                select(composer_inflight_requests_table.c.request_token, composer_inflight_requests_table.c.owner_instance_id).where(
                    composer_inflight_requests_table.c.session_id == session_id,
                    composer_inflight_requests_table.c.expires_at > now,
                )
            ).all()
            if lease is not None and not any(
                row.request_token == lease.request_token and row.owner_instance_id == self._owner_instance_id for row in active_tokens
            ):
                raise ComposerRequestLeaseLost("Composer request cannot replay progress")
            # A replay's own request lifecycle is allowed; other queued work wins.
            if any(lease is None or row.request_token != lease.request_token for row in active_tokens):
                return None
            snapshot = _snapshot(session_id, request_id, event, now)
            conn.execute(delete(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == session_id))
            conn.execute(
                insert(composer_progress_snapshots_table).values(
                    session_id=session_id,
                    identity_id=user_id,
                    generation=uuid4().hex,
                    request_token=None,
                    request_id=request_id,
                    snapshot_json=_encode(snapshot),
                    updated_at=now,
                    expires_at=now + timedelta(seconds=self._snapshot_ttl_seconds),
                )
            )
        return snapshot

    def get_latest(self, session_id: str, user_id: str) -> ComposerProgressSnapshot:
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            if conn.execute(_ownership_query(session_id, user_id)).one_or_none() is None:
                raise PermissionError("Composer progress session is unavailable")
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            row = conn.execute(
                select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == session_id)
            ).one_or_none()
            count = conn.execute(
                select(func.count())
                .select_from(composer_inflight_requests_table)
                .where(
                    composer_inflight_requests_table.c.session_id == session_id,
                    composer_inflight_requests_table.c.expires_at > now,
                )
            ).scalar_one()
            return _read_snapshot(row, session_id, user_id, now, count)

    def list_active(self, user_id: str) -> tuple[ComposerProgressSnapshot, ...]:
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            session_ids = (
                conn.execute(
                    select(sessions_table.c.id)
                    .where(sessions_table.c.user_id == user_id, sessions_table.c.archived_at.is_(None))
                    .order_by(sessions_table.c.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            now = _database_clock_value(conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one())
            result: list[ComposerProgressSnapshot] = []
            for session_id in session_ids:
                count = conn.execute(
                    select(func.count())
                    .select_from(composer_inflight_requests_table)
                    .where(
                        composer_inflight_requests_table.c.session_id == session_id,
                        composer_inflight_requests_table.c.expires_at > now,
                    )
                ).scalar_one()
                if not count:
                    continue
                row = conn.execute(
                    select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == session_id)
                ).one_or_none()
                result.append(_read_snapshot(row, session_id, user_id, now, count))
            return tuple(sorted(result, key=lambda snapshot: snapshot.updated_at))

    def clear(self, session_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            if conn.execute(_identity_query(user_id)).one_or_none() is None:
                raise PermissionError("Composer progress identity is inactive")
            session = conn.execute(
                select(sessions_table.c.user_id).where(sessions_table.c.id == session_id).with_for_update()
            ).one_or_none()
            # Archiving a session without durable history physically deletes it;
            # foreign-key cascades have already removed its progress in that case.
            if session is None:
                return
            if session.user_id != user_id:
                raise PermissionError("Composer progress session is unavailable")
            conn.execute(
                delete(composer_progress_snapshots_table).where(
                    composer_progress_snapshots_table.c.session_id == session_id,
                    composer_progress_snapshots_table.c.identity_id == user_id,
                )
            )


class DatabaseComposerProgressRegistry:
    """Async route facade; all database work stays off the event loop."""

    def __init__(self, authority: SessionComposerProgressAuthority) -> None:
        self._authority = authority

    async def start_request(self, session_id: str, user_id: str) -> ComposerRequestLease:
        admission = asyncio.create_task(
            run_sync_in_worker(self._authority.begin_request, session_id, user_id),
            name="composer-progress-admission",
        )
        try:
            return await asyncio.shield(admission)
        except asyncio.CancelledError:
            # The database worker can commit after its caller is cancelled.
            # Join its result and release that exact lease before propagating
            # cancellation, including repeated cancellation during teardown.
            lease = await _join_cancelled_request_task(admission)
            cleanup = asyncio.create_task(
                run_sync_in_worker(self._authority.end_request, lease),
                name="composer-progress-cancelled-admission-cleanup",
            )
            await _join_cancelled_request_task(cleanup)
            raise

    async def finish_request(self, lease: ComposerRequestLease) -> None:
        await run_sync_in_worker(self._authority.end_request, lease)

    async def renew_request(self, lease: ComposerRequestLease) -> None:
        await run_sync_in_worker(self._authority.heartbeat_request, lease)

    async def claim_request(
        self, *, session_id: str, request_id: str | None, user_id: str, lease: ComposerRequestLease
    ) -> ComposerProgressSink:
        if lease.session_id != session_id or lease.user_id != user_id:
            raise ValueError("Composer request lease does not match the request")
        generation = await run_sync_in_worker(self._authority.bind_request, lease, request_id)

        async def publish(event: ComposerProgressEvent) -> None:
            await run_sync_in_worker(self._authority.publish, lease, generation, request_id, event)

        return publish

    async def publish_replay_if_unclaimed(
        self,
        *,
        session_id: str,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent,
        lease: ComposerRequestLease | None = None,
    ) -> ComposerProgressSnapshot | None:
        return await run_sync_in_worker(self._authority.replay, session_id, user_id, request_id, event, lease=lease)

    async def get_latest(self, session_id: str, user_id: str) -> ComposerProgressSnapshot:
        return await run_sync_in_worker(self._authority.get_latest, session_id, user_id)

    async def list_active(self, *, user_id: str) -> tuple[ComposerProgressSnapshot, ...]:
        return await run_sync_in_worker(self._authority.list_active, user_id)

    async def clear(self, session_id: str, user_id: str) -> None:
        await run_sync_in_worker(self._authority.clear, session_id, user_id)


async def _join_cancelled_request_task[T](task: asyncio.Task[T]) -> T:
    """Drain owned work despite repeated cancellation, surfacing its outcome."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()
