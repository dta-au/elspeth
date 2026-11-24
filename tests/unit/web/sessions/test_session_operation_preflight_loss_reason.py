"""The shared session-operation write preflight names why an exact context lost authority.

Identity is decided first (operation, token, epoch, kind, unreleased), then the
deadline: the exact current fence past its deadline reports LEASE_EXPIRED, and
every other miss stays TOKEN_MISMATCH. The refusal itself is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy import Engine, select

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'preflight-reason.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    try:
        yield engine
    finally:
        engine.dispose()


async def _compose_context(service: SessionServiceImpl) -> SessionOperationContext:
    session = await service.create_session("alice", "preflight", "local")
    return await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )


def _expiry(engine: Engine, context: SessionOperationContext) -> datetime:
    with engine.connect() as conn:
        expiry: datetime = conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(
                session_operation_fences_table.c.session_id == context.fence.session_id
            )
        ).scalar_one()
    return expiry


def _with_fence(context: SessionOperationContext, *, lease_token: str | None = None, epoch: int | None = None) -> SessionOperationContext:
    fence = context.fence
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=fence.session_id,
            operation_id=fence.operation_id,
            lease_token=fence.lease_token if lease_token is None else lease_token,
            operation_epoch=fence.operation_epoch if epoch is None else epoch,
        ),
        operation_kind=context.operation_kind,
    )


def _preflight(
    service: SessionServiceImpl,
    engine: Engine,
    context: SessionOperationContext,
    *,
    now: datetime,
    expected_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
) -> None:
    with engine.connect() as conn:
        service._require_session_operation_context_on_connection(
            conn,
            context,
            session_id=context.fence.session_id,
            expected_kind=expected_kind,
            now=now,
            audit_only=True,
        )


def _reason(
    service: SessionServiceImpl,
    engine: Engine,
    context: SessionOperationContext,
    *,
    now: datetime,
    expected_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
) -> FenceLossReason:
    with pytest.raises(SessionOperationFenceLost) as lost:
        _preflight(service, engine, context, now=now, expected_kind=expected_kind)
    return lost.value.reason


@pytest.mark.asyncio
async def test_the_exact_fence_past_its_deadline_reports_lease_expired(engine: Engine) -> None:
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.preflight"))
    context = await _compose_context(service)
    expiry = _expiry(engine, context)

    _preflight(service, engine, context, now=expiry - timedelta(seconds=1))
    assert _reason(service, engine, context, now=expiry) is FenceLossReason.LEASE_EXPIRED
    assert _reason(service, engine, context, now=expiry + timedelta(seconds=1)) is FenceLossReason.LEASE_EXPIRED


@pytest.mark.asyncio
async def test_every_other_miss_stays_token_mismatch_live_or_past_the_deadline(engine: Engine) -> None:
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.preflight"))
    context = await _compose_context(service)
    expiry = _expiry(engine, context)
    foreign_token = _with_fence(context, lease_token="not-this-lease")
    stale_epoch = _with_fence(context, epoch=context.fence.operation_epoch + 1)

    for now in (expiry - timedelta(seconds=1), expiry + timedelta(seconds=1)):
        assert _reason(service, engine, foreign_token, now=now) is FenceLossReason.TOKEN_MISMATCH
        assert _reason(service, engine, stale_epoch, now=now) is FenceLossReason.TOKEN_MISMATCH
        # The exact fence asked for under another kind is a mismatch, expired or not.
        assert _reason(service, engine, context, now=now, expected_kind=SessionOperationKind.PROPOSAL) is FenceLossReason.TOKEN_MISMATCH


@pytest.mark.asyncio
async def test_a_released_fence_stays_token_mismatch_past_its_deadline(engine: Engine) -> None:
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.preflight"))
    context = await _compose_context(service)
    await service._run_sync(lambda: service.session_operation_authority.release(context))
    expiry = _expiry(engine, context)

    assert _reason(service, engine, context, now=expiry + timedelta(seconds=1)) is FenceLossReason.TOKEN_MISMATCH
