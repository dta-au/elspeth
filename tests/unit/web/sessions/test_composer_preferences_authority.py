from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import proposal_events_table, session_operation_fences_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.mark.asyncio
async def test_update_composer_preferences_accepts_live_compose_context() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-preferences-authority"),
    )
    session_id = (await service.create_session("alice", "Composer preferences authority", "local")).id
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        transition = await service.update_composer_preferences(
            session_id,
            trust_mode="explicit_approve",
            density_default="medium",
            actor="user:alice",
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)

    assert transition.prior.trust_mode == "auto_commit"
    assert transition.current.trust_mode == "explicit_approve"
    assert transition.current.density_default == "medium"
    events = await service.list_proposal_events(session_id)
    assert len(events) == 1
    assert events[0].event_type == "trust_mode.changed"
    assert events[0].payload == {
        "trust_mode": "explicit_approve",
        "prior_trust_mode": "auto_commit",
        "density_default": "medium",
    }


@pytest.mark.asyncio
async def test_stale_compose_predecessor_changes_no_preferences_or_events() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    first = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-preferences-predecessor"),
        owner_instance_id="composer-preferences-first",
    )
    second = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-preferences-successor"),
        owner_instance_id="composer-preferences-second",
    )
    session_id = (await first.create_session("alice", "Composer preferences takeover", "local")).id
    with engine.connect() as connection:
        before = connection.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()

    predecessor = await first._run_sync(
        lambda: first.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=first.session_operation_owner_instance_id,
            lease_seconds=first.session_operation_lease_seconds,
        )
    )
    with engine.begin() as connection:
        connection.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    successor = await second._run_sync(
        lambda: second.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(SessionOperationFenceLost):
            await first.update_composer_preferences(
                session_id,
                trust_mode="explicit_approve",
                density_default="medium",
                actor="user:alice",
                session_operation_context=predecessor,
            )
    finally:
        await second._run_sync(second.session_operation_authority.release, successor)

    with engine.connect() as connection:
        after = connection.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()
        event_count = connection.execute(
            select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.session_id == str(session_id))
        ).scalar_one()
    assert after.trust_mode == before.trust_mode
    assert after.density_default == before.density_default
    assert after.updated_at == before.updated_at
    assert event_count == 0
