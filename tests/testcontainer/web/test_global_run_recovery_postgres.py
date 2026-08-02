"""PostgreSQL proofs for globally coordinated run recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, insert, update

from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table
from elspeth.web.sessions.protocol import CompositionStateData, RunRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture()
def deployment(
    external_deployment_postgres_url: str,
) -> Iterator[tuple[Engine, Engine, SessionServiceImpl, SessionServiceImpl]]:
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    first = SessionServiceImpl(
        first_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-run-recovery-a"),
        owner_instance_id=f"run-recovery-a-{uuid4()}",
    )
    second = SessionServiceImpl(
        second_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-run-recovery-b"),
        owner_instance_id=f"run-recovery-b-{uuid4()}",
    )
    try:
        yield first_engine, second_engine, first, second
    finally:
        first_engine.dispose()
        second_engine.dispose()


def _register_live_instance(engine: Engine, instance_id: str) -> None:
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="global-run-recovery",
                session_epoch=43,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:global-run-recovery",
                revision_label="global-run-recovery",
                state="active",
                started_at=now,
                last_heartbeat_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )


def _expire_fence(engine: Engine, session_id: UUID) -> None:
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )


def _expire_instance(engine: Engine, instance_id: str) -> None:
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )


async def _create_running_run(service: SessionServiceImpl) -> tuple[RunRecord, SessionOperationContext]:
    session = await service.create_session(str(uuid4()), "Pipeline", "local")
    compose_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, compose_context)
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    run = await service.create_run(session.id, state.id, session_operation_context=context)
    await service.update_run_status(run.id, "running", session_operation_context=context)
    return run, context


@pytest.mark.asyncio
async def test_recovery_requires_both_fence_and_membership_expiry_and_has_one_winner(deployment) -> None:
    first_engine, _second_engine, first, second = deployment
    _register_live_instance(first_engine, first.session_operation_owner_instance_id)
    run, _context = await _create_running_run(first)
    _expire_fence(first_engine, run.session_id)

    assert await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered") == []
    assert (await second.get_run(run.id)).status == "running"

    _expire_instance(first_engine, first.session_operation_owner_instance_id)
    contenders = await asyncio.gather(
        second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered"),
        second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered"),
    )

    assert [record.id for batch in contenders for record in batch] == [run.id]
    assert (await second.get_run(run.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_expired_fence_with_missing_membership_fails_closed(deployment) -> None:
    first_engine, _second_engine, first, second = deployment
    run, _context = await _create_running_run(first)
    _expire_fence(first_engine, run.session_id)

    assert await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered") == []
    assert (await second.get_run(run.id)).status == "running"


@pytest.mark.asyncio
async def test_new_operation_acquisition_and_recovery_serialize_without_overwrite(deployment) -> None:
    first_engine, _second_engine, first, second = deployment
    _register_live_instance(first_engine, first.session_operation_owner_instance_id)
    run, _context = await _create_running_run(first)
    _expire_fence(first_engine, run.session_id)
    _expire_instance(first_engine, first.session_operation_owner_instance_id)

    cleanup_task = asyncio.create_task(second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered"))
    acquire_task = asyncio.create_task(
        asyncio.to_thread(
            second.session_operation_authority.acquire,
            session_id=run.session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    cancelled, successor = await asyncio.gather(cleanup_task, acquire_task)

    if cancelled:
        assert [record.id for record in cancelled] == [run.id]
        assert (await second.get_run(run.id)).status == "cancelled"
    else:
        assert (await second.get_run(run.id)).status == "running"
        assert await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered") == []

    second.session_operation_authority.release(successor)
    final = await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered")
    assert [record.id for record in [*cancelled, *final]] == [run.id]
    assert (await second.get_run(run.id)).status == "cancelled"
