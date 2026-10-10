"""SQL authority for privacy-preserving shared sliding-window quotas."""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, final
from uuid import uuid4

from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import rate_limit_buckets_table, rate_limit_events_table
from elspeth.web.sessions.time_normalization import restore_utc

RateLimitScope = Literal["composer", "write", "auth", "audit_readiness"]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Admission outcome; retry seconds are rounded up to the expiry boundary."""

    allowed: bool
    retry_after: int


def _database_now(conn: Connection) -> datetime:
    if conn.dialect.name == "sqlite":
        return database_now(conn)
    value = conn.execute(select(func.clock_timestamp())).scalar_one()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Rate limit database clock must return an aware datetime")
    return value.astimezone(UTC)


def _validate_request(scope: RateLimitScope, subject: str, limit: int, window_seconds: int) -> None:
    if scope not in {"composer", "write", "auth", "audit_readiness"}:
        raise ValueError("Unknown rate limit scope")
    if type(subject) is not str or not subject:
        raise ValueError("Rate limit subject must be a nonempty string")
    if type(limit) is not int or limit <= 0 or type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("Rate limit and window must be positive integers")


@final
class RepositoryRateLimitAuthority:
    """Serialize budgets by PostgreSQL bucket locks or SQLite write exclusion.

    Standalone admission owns its transaction. Composer admission instead uses
    the caller's exact sessions connection to commit a job and its quota event
    together. Neither path returns a connection or commits a caller transaction.
    Cleanup runs before admission locks, in its own bounded transaction.
    SQLite callers use the sessions engine's explicit BEGIN IMMEDIATE writes.
    """

    __slots__ = ("_engine", "_key")
    _CLEANUP_BUCKETS = 32

    def __init__(self, engine: Engine, *, signing_key: bytes) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("Shared rate limiting requires PostgreSQL or SQLite")
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("Rate limit signing key must contain at least 32 bytes")
        self._engine = engine
        self._key = hmac.digest(signing_key, b"elspeth:web:rate-limits:v1", hashlib.sha256)

    def _cleanup(self) -> None:
        with self._engine.begin() as conn:
            now = _database_now(conn)
            query = (
                select(rate_limit_buckets_table.c.subject_digest)
                .where(rate_limit_buckets_table.c.expires_at <= now)
                .order_by(rate_limit_buckets_table.c.expires_at, rate_limit_buckets_table.c.subject_digest)
                .limit(self._CLEANUP_BUCKETS)
            )
            if conn.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            expired = conn.execute(query).scalars().all()
            if expired:
                # The FK cascades events while the bucket locks exclude admission.
                conn.execute(delete(rate_limit_buckets_table).where(rate_limit_buckets_table.c.subject_digest.in_(expired)))

    def admit(self, *, scope: RateLimitScope, subject: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        """Atomically prune, count and admit against fresh post-lock database time."""
        _validate_request(scope, subject, limit, window_seconds)
        self._cleanup()
        with self._engine.begin() as conn:
            return self.admit_on_connection(conn, scope=scope, subject=subject, limit=limit, window_seconds=window_seconds)

    def prepare_composer_admission(self, engine: Engine) -> None:
        """Validate database ownership and finish cleanup before session locking."""
        if engine is not self._engine:
            raise ValueError("Composer quota must use the same sessions engine")
        self._cleanup()

    def admit_on_connection(
        self, conn: Connection, *, scope: RateLimitScope, subject: str, limit: int, window_seconds: int = 60
    ) -> RateLimitDecision:
        """Consume quota in this exact existing transaction without cleanup/commit."""
        if conn.engine is not self._engine or not conn.in_transaction():
            raise ValueError("Quota admission requires an active transaction on the same sessions engine")
        _validate_request(scope, subject, limit, window_seconds)
        scope_key = hmac.digest(self._key, scope.encode("ascii"), hashlib.sha256)
        digest = hmac.new(scope_key, subject.encode("utf-8"), hashlib.sha256).hexdigest()
        created_at = _database_now(conn)
        bucket_insert = (
            pg_insert(rate_limit_buckets_table) if conn.dialect.name == "postgresql" else sqlite_insert(rate_limit_buckets_table)
        )
        conn.execute(
            bucket_insert.values(
                subject_digest=digest,
                window_seconds=window_seconds,
                updated_at=created_at,
                expires_at=created_at + timedelta(seconds=window_seconds),
            )
            # A no-op UPDATE acquires the existing row lock too, so cleanup
            # cannot delete the bucket between this upsert and the read.
            .on_conflict_do_update(index_elements=[rate_limit_buckets_table.c.subject_digest], set_={"subject_digest": digest})
        )
        bucket_query = select(rate_limit_buckets_table.c.window_seconds).where(rate_limit_buckets_table.c.subject_digest == digest)
        if conn.dialect.name == "postgresql":
            bucket_query = bucket_query.with_for_update()
        bucket = conn.execute(bucket_query).one()
        if bucket.window_seconds != window_seconds:
            raise ValueError("Rate limit window disagrees with active bucket")
        now = _database_now(conn)
        conn.execute(
            delete(rate_limit_events_table).where(
                rate_limit_events_table.c.subject_digest == digest, rate_limit_events_table.c.expires_at <= now
            )
        )
        count, earliest = conn.execute(
            select(func.count(), func.min(rate_limit_events_table.c.expires_at)).where(rate_limit_events_table.c.subject_digest == digest)
        ).one()
        if count >= limit:
            if not isinstance(earliest, datetime) or (conn.dialect.name == "postgresql" and earliest.tzinfo is None):
                raise RuntimeError("Rate limit event expiry must be a database datetime")
            return RateLimitDecision(False, max(1, math.ceil((restore_utc(earliest) - now).total_seconds())))
        expires_at = now + timedelta(seconds=window_seconds)
        conn.execute(
            insert(rate_limit_events_table).values(event_id=str(uuid4()), subject_digest=digest, occurred_at=now, expires_at=expires_at)
        )
        conn.execute(
            update(rate_limit_buckets_table)
            .where(rate_limit_buckets_table.c.subject_digest == digest)
            .values(updated_at=now, expires_at=expires_at)
        )
        return RateLimitDecision(True, 0)


class ComposerQuotaExceeded(Exception):
    """A fresh job and its quota event must both roll back on this refusal."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Composer rate limit exceeded")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ComposerQuotaAdmission:
    """Mandatory composer-scoped quota configuration for durable admission."""

    authority: RepositoryRateLimitAuthority
    limit: int

    def __post_init__(self) -> None:
        if type(self.authority) is not RepositoryRateLimitAuthority or type(self.limit) is not int or self.limit <= 0:
            raise ValueError("Composer quota requires an exact SQL authority and positive limit")

    def prepare(self, engine: Engine) -> None:
        self.authority.prepare_composer_admission(engine)

    def check_on_connection(self, conn: Connection, subject: str) -> None:
        decision = self.authority.admit_on_connection(conn, scope="composer", subject=subject, limit=self.limit)
        if not decision.allowed:
            raise ComposerQuotaExceeded(decision.retry_after)
