"""Handle-free PostgreSQL authority for privacy-preserving cluster quotas."""

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

from elspeth.web.sessions.models import rate_limit_buckets_table, rate_limit_events_table

RateLimitScope = Literal["composer", "write", "auth"]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Admission outcome; retry seconds are rounded up to the expiry boundary."""

    allowed: bool
    retry_after: int


def _database_now(conn: Connection) -> datetime:
    value = conn.execute(select(func.clock_timestamp())).scalar_one()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Rate limit database clock must return an aware datetime")
    return value.astimezone(UTC)


@final
class RepositoryRateLimitAuthority:
    """Serialize each subject budget under a durable bucket row lock.

    No connection or transaction escapes this authority. Cleanup uses a separate
    bounded transaction, avoiding lock-order cycles with admission. Database
    errors propagate to the adapter, which refuses the request without fallback.
    """

    __slots__ = ("_engine", "_key")
    _CLEANUP_BUCKETS = 32

    def __init__(self, engine: Engine, *, signing_key: bytes) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Shared rate limiting requires PostgreSQL")
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("Rate limit signing key must contain at least 32 bytes")
        self._engine = engine
        self._key = hmac.digest(signing_key, b"elspeth:web:rate-limits:v1", hashlib.sha256)

    def _cleanup(self) -> None:
        with self._engine.begin() as conn:
            now = _database_now(conn)
            expired = (
                conn.execute(
                    select(rate_limit_buckets_table.c.subject_digest)
                    .where(rate_limit_buckets_table.c.expires_at <= now)
                    .order_by(rate_limit_buckets_table.c.expires_at, rate_limit_buckets_table.c.subject_digest)
                    .limit(self._CLEANUP_BUCKETS)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if expired:
                # The FK cascades events while the bucket locks exclude admission.
                conn.execute(delete(rate_limit_buckets_table).where(rate_limit_buckets_table.c.subject_digest.in_(expired)))

    def admit(self, *, scope: RateLimitScope, subject: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        """Atomically prune, count and admit against fresh post-lock database time."""
        if scope not in {"composer", "write", "auth"}:
            raise ValueError("Unknown rate limit scope")
        if type(subject) is not str or not subject:
            raise ValueError("Rate limit subject must be a nonempty string")
        if type(limit) is not int or limit <= 0 or type(window_seconds) is not int or window_seconds <= 0:
            raise ValueError("Rate limit and window must be positive integers")
        scope_key = hmac.digest(self._key, scope.encode("ascii"), hashlib.sha256)
        digest = hmac.new(scope_key, subject.encode("utf-8"), hashlib.sha256).hexdigest()
        self._cleanup()
        with self._engine.begin() as conn:
            created_at = _database_now(conn)
            conn.execute(
                pg_insert(rate_limit_buckets_table)
                .values(
                    subject_digest=digest,
                    window_seconds=window_seconds,
                    updated_at=created_at,
                    expires_at=created_at + timedelta(seconds=window_seconds),
                )
                # A no-op UPDATE acquires the existing row lock too, so cleanup
                # cannot delete the bucket between this upsert and the read.
                .on_conflict_do_update(index_elements=[rate_limit_buckets_table.c.subject_digest], set_={"subject_digest": digest})
            )
            bucket = conn.execute(
                select(rate_limit_buckets_table.c.window_seconds)
                .where(rate_limit_buckets_table.c.subject_digest == digest)
                .with_for_update()
            ).one()
            if bucket.window_seconds != window_seconds:
                raise ValueError("Rate limit window disagrees with active bucket")
            now = _database_now(conn)
            conn.execute(
                delete(rate_limit_events_table).where(
                    rate_limit_events_table.c.subject_digest == digest, rate_limit_events_table.c.expires_at <= now
                )
            )
            count, earliest = conn.execute(
                select(func.count(), func.min(rate_limit_events_table.c.expires_at)).where(
                    rate_limit_events_table.c.subject_digest == digest
                )
            ).one()
            if count >= limit:
                if not isinstance(earliest, datetime) or earliest.tzinfo is None:
                    raise RuntimeError("Rate limit event expiry must be an aware datetime")
                return RateLimitDecision(False, max(1, math.ceil((earliest - now).total_seconds())))
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
