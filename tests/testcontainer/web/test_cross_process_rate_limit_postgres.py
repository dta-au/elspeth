"""Independent-process shared quota and database-clock boundary proofs."""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Iterator
from datetime import timedelta
from multiprocessing.connection import Connection
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, update
from tests.helpers.postgres_target import postgres_test_target

from elspeth.web.coordination.rate_limit_authority import RepositoryRateLimitAuthority
from elspeth.web.middleware.rate_limit import SharedRateLimiter
from elspeth.web.sessions.models import rate_limit_buckets_table, rate_limit_events_table

pytestmark = pytest.mark.testcontainer
_KEY = b"rate-limit-test-domain-key-32bytes"


@pytest.fixture(scope="module")
def rate_url() -> Iterator[str]:
    with postgres_test_target() as url:
        engine = create_engine(url)
        rate_limit_buckets_table.create(engine)
        rate_limit_events_table.create(engine)
        engine.dispose()
        yield url


def _admit_process(url: str, subject: str, pipe: Connection) -> None:
    engine = create_engine(url)
    try:
        limiter = SharedRateLimiter(3, authority=RepositoryRateLimitAuthority(engine, signing_key=_KEY), scope="composer")
        pipe.send("ready")
        pipe.recv()
        results = []
        for _ in range(3):
            try:
                asyncio.run(limiter.check(subject))
                results.append(200)
            except HTTPException as exc:
                results.append(exc.status_code)
        pipe.send(results)
    finally:
        engine.dispose()
        pipe.close()


def test_two_processes_share_budget(rate_url: str) -> None:
    ctx = multiprocessing.get_context("spawn")
    subject = str(uuid4())
    pairs = [ctx.Pipe() for _ in range(2)]
    processes = [ctx.Process(target=_admit_process, args=(rate_url, subject, child)) for _, child in pairs]
    try:
        for process in processes:
            process.start()
        for parent, _ in pairs:
            assert parent.poll(30)
            assert parent.recv() == "ready"
        for parent, _ in pairs:
            parent.send("go")
        results = []
        for parent, _ in pairs:
            assert parent.poll(30)
            results.extend(parent.recv())
        assert sorted(results) == [200, 200, 200, 429, 429, 429]
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()
        for parent, child in pairs:
            parent.close()
            child.close()


def test_scopes_privacy_expiration_and_retry(rate_url: str) -> None:
    engine = create_engine(rate_url)
    try:
        authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
        subject = f"private-ip-{uuid4()}"
        for scope in ("composer", "write", "auth"):
            assert authority.admit(scope=scope, subject=subject, limit=1).allowed
            denied = authority.admit(scope=scope, subject=subject, limit=1)
            assert not denied.allowed
            assert 59 <= denied.retry_after <= 60
        with engine.begin() as conn:
            buckets = conn.execute(select(rate_limit_buckets_table)).all()
            assert subject not in repr(buckets)
            digests = [row.subject_digest for row in buckets]
            assert len(set(digests)) == len(digests)
            assert all(len(digest) == 64 for digest in digests)
            now = conn.execute(select(func.clock_timestamp())).scalar_one()
            conn.execute(update(rate_limit_events_table).values(expires_at=now - timedelta(seconds=1)))
        assert authority.admit(scope="composer", subject=subject, limit=1).allowed
    finally:
        engine.dispose()


def _boundary_admit(url: str, subject: str, application_name: str, pipe: Connection) -> None:
    engine = create_engine(url, connect_args={"application_name": application_name})
    try:
        authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
        pipe.send("ready")
        pipe.recv()
        decision = authority.admit(scope="auth", subject=subject, limit=1, window_seconds=2)
        pipe.send((decision.allowed, decision.retry_after))
    finally:
        engine.dispose()
        pipe.close()


def test_waiting_processes_use_post_lock_clock_at_expiry(rate_url: str) -> None:
    """Both callers wait across expiry; only one may spend restored capacity."""
    import time

    from sqlalchemy import text

    engine = create_engine(rate_url)
    subject = str(uuid4())
    app_name = f"rate-boundary-{uuid4().hex}"
    authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
    assert authority.admit(scope="auth", subject=subject, limit=1, window_seconds=2).allowed
    ctx = multiprocessing.get_context("spawn")
    pairs = [ctx.Pipe() for _ in range(2)]
    processes = [ctx.Process(target=_boundary_admit, args=(rate_url, subject, app_name, child)) for _, child in pairs]
    try:
        for process in processes:
            process.start()
        for parent, _ in pairs:
            assert parent.poll(30)
            assert parent.recv() == "ready"
        with engine.begin() as locked:
            # The only two-second bucket belongs to this test's unique subject.
            digest = locked.execute(
                select(rate_limit_buckets_table.c.subject_digest).where(rate_limit_buckets_table.c.window_seconds == 2).with_for_update()
            ).scalar_one()
            expires = locked.execute(select(func.clock_timestamp())).scalar_one() + timedelta(seconds=2)
            locked.execute(
                update(rate_limit_buckets_table).where(rate_limit_buckets_table.c.subject_digest == digest).values(expires_at=expires)
            )
            locked.execute(
                update(rate_limit_events_table).where(rate_limit_events_table.c.subject_digest == digest).values(expires_at=expires)
            )
            for parent, _ in pairs:
                parent.send("go")
            deadline = time.monotonic() + 20
            while True:
                with engine.connect() as observer:
                    waiting = observer.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :name AND wait_event_type = 'Lock'"),
                        {"name": app_name},
                    ).scalar_one()
                if waiting == 2:
                    break
                assert time.monotonic() < deadline, "Independent callers did not both reach the database lock"
                time.sleep(0.02)
            locked.execute(select(func.pg_sleep(2.1)))
        decisions = []
        for parent, _ in pairs:
            assert parent.poll(30)
            decisions.append(parent.recv())
        assert sorted(decisions) == [(False, 2), (True, 0)]
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()
        for parent, child in pairs:
            parent.close()
            child.close()
        engine.dispose()


def test_cleanup_is_bounded_and_cascades_only_expired_buckets(rate_url: str) -> None:
    import hashlib

    from sqlalchemy import delete, insert

    engine = create_engine(rate_url)
    try:
        with engine.begin() as conn:
            conn.execute(delete(rate_limit_buckets_table))
            now = conn.execute(select(func.clock_timestamp())).scalar_one()
            expired = now - timedelta(seconds=1)
            for index in range(40):
                digest = hashlib.sha256(str(index).encode()).hexdigest()
                conn.execute(
                    insert(rate_limit_buckets_table).values(
                        subject_digest=digest, window_seconds=60, updated_at=expired, expires_at=expired
                    )
                )
                conn.execute(
                    insert(rate_limit_events_table).values(
                        event_id=str(uuid4()), subject_digest=digest, occurred_at=expired, expires_at=expired
                    )
                )
        authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
        assert authority.admit(scope="auth", subject="active", limit=1).allowed
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(rate_limit_buckets_table)).scalar_one() == 9
            assert conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one() == 9
        # A second request clears the rest, retaining the live budget.
        assert not authority.admit(scope="auth", subject="active", limit=1).allowed
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(rate_limit_buckets_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one() == 1
    finally:
        engine.dispose()


def test_retry_rounds_up_and_expiry_restores_capacity(rate_url: str) -> None:
    engine = create_engine(rate_url)
    try:
        with engine.begin() as conn:
            from sqlalchemy import delete

            conn.execute(delete(rate_limit_buckets_table))
        authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
        assert authority.admit(scope="write", subject="retry-boundary", limit=1).allowed
        with engine.begin() as conn:
            now = conn.execute(select(func.clock_timestamp())).scalar_one()
            conn.execute(update(rate_limit_events_table).values(expires_at=now + timedelta(seconds=0.9)))
        decision = authority.admit(scope="write", subject="retry-boundary", limit=1)
        assert not decision.allowed
        assert decision.retry_after == 1
        with engine.begin() as conn:
            now = conn.execute(select(func.clock_timestamp())).scalar_one()
            conn.execute(update(rate_limit_events_table).values(expires_at=now))
        assert authority.admit(scope="write", subject="retry-boundary", limit=1).allowed
    finally:
        engine.dispose()


def test_postgres_readonly_refusal_never_uses_local_budget(rate_url: str) -> None:
    engine = create_engine(rate_url, execution_options={"postgresql_readonly": True})
    try:
        authority = RepositoryRateLimitAuthority(engine, signing_key=_KEY)
        limiter = SharedRateLimiter(10, authority=authority, scope="auth")
        with engine.connect() as conn:
            before = conn.execute(select(rate_limit_events_table).order_by(rate_limit_events_table.c.event_id)).all()
        for _ in range(2):
            with pytest.raises(HTTPException) as caught:
                asyncio.run(limiter.check("private-ip-never-persisted"))
            assert caught.value.status_code == 503
            assert caught.value.detail == "Rate limit service unavailable"
        with engine.connect() as conn:
            after = conn.execute(select(rate_limit_events_table).order_by(rate_limit_events_table.c.event_id)).all()
        assert after == before
    finally:
        engine.dispose()
