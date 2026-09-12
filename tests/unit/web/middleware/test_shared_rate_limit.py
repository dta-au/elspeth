"""Shared adapter refusal and nominal ownership contracts."""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from elspeth.web.coordination.rate_limit_authority import RepositoryRateLimitAuthority
from elspeth.web.middleware.rate_limit import SharedRateLimiter


def test_shared_authority_rejects_sqlite() -> None:
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(ValueError, match="PostgreSQL"):
            RepositoryRateLimitAuthority(engine, signing_key=b"s" * 32)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_database_refusal_is_unavailable_without_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("postgresql+psycopg2://")
    authority = RepositoryRateLimitAuthority(engine, signing_key=b"s" * 32)
    limiter = SharedRateLimiter(1, authority=authority, scope="auth")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise OperationalError("private query", {}, RuntimeError("private-ip"))

    monkeypatch.setattr(engine, "connect", unavailable)
    try:
        for _ in range(2):
            with pytest.raises(HTTPException) as caught:
                await limiter.check("private-ip")
            assert caught.value.status_code == 503
            assert caught.value.detail == "Rate limit service unavailable"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_worker_saturation_refuses_before_database_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web import async_workers

    engine = create_engine("postgresql+psycopg2://")
    authority = RepositoryRateLimitAuthority(engine, signing_key=b"s" * 32)
    limiter = SharedRateLimiter(1, authority=authority, scope="auth")

    def saturated() -> bool:
        return False

    def unexpected_connection(*args: object, **kwargs: object) -> None:
        pytest.fail("Saturated worker pool must not submit database work")

    monkeypatch.setattr(async_workers, "_try_admit", saturated)
    monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(engine, "connect", unexpected_connection)
    try:
        with pytest.raises(HTTPException) as caught:
            await limiter.check("private-ip")
        assert caught.value.status_code == 503
        assert caught.value.detail == "Rate limit service unavailable"
    finally:
        engine.dispose()
