"""PostgreSQL proofs for web-instance membership: a dead owner is taken over, a live one is not.

Probe P3 of the replica > 1 acceptances in one container: two processes
register through the production writer, one is partitioned (its heartbeat
simply never arrives — no row is hand-expired), and the survivor may cancel
the dead owner's run and take its session fence only after BOTH the fence
lease and the membership lease have expired on the database clock. A live
heartbeat keeps a process unstealable even after its fence lease lapses; a
clean ``stop`` makes takeover immediate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, select

from elspeth.web.coordination.contracts import InstanceState, SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceIdentity,
    current_compatibility_key,
)
from elspeth.web.coordination.membership_lifecycle import RegisteredWebInstanceMembership
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table
from elspeth.web.sessions.protocol import CompositionStateData, RunRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer

_SHORT_LEASE_SECONDS = 2
_PAST_BOTH_LEASES_SECONDS = 4.0


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
        log=structlog.get_logger("test.pg-membership-a"),
        owner_instance_id=f"membership-a-{uuid4()}",
        session_operation_lease_seconds=_SHORT_LEASE_SECONDS,
    )
    second = SessionServiceImpl(
        second_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-membership-b"),
        owner_instance_id=f"membership-b-{uuid4()}",
    )
    try:
        yield first_engine, second_engine, first, second
    finally:
        first_engine.dispose()
        second_engine.dispose()


def _identity(instance_id: str) -> WebInstanceIdentity:
    return WebInstanceIdentity(
        instance_id=instance_id,
        deployment_target="testcontainer",
        deployment_generation="membership-probe",
        compatibility_key=current_compatibility_key(),
        image_digest="sha256:membership-probe",
        revision_label="membership-probe",
    )


def _membership_row(engine: Engine, instance_id: str) -> tuple[Any, datetime]:
    with engine.connect() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        row = conn.execute(select(web_instances_table).where(web_instances_table.c.instance_id == instance_id)).one()
    return row, now


def _fence_row(engine: Engine, session_id: UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id))
        ).one()


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


def _acquire_as(service: SessionServiceImpl, session_id: UUID) -> SessionOperationContext:
    return service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )


@pytest.mark.asyncio
async def test_partitioned_owner_is_taken_over_only_after_both_leases_expire(deployment) -> None:
    first_engine, second_engine, first, second = deployment
    owner_id = first.session_operation_owner_instance_id
    RepositoryWebInstanceMembershipAuthority(first_engine).register(_identity(owner_id), lease_seconds=_SHORT_LEASE_SECONDS)
    RepositoryWebInstanceMembershipAuthority(second_engine).register(
        _identity(second.session_operation_owner_instance_id), lease_seconds=300
    )
    run, _context = await _create_running_run(first)

    # Before expiry: the fence is live, so both the survivor's acquire and its
    # recovery sweep refuse.
    with pytest.raises(SessionOperationConflictError):
        await asyncio.to_thread(_acquire_as, second, run.session_id)
    assert await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered") == []
    assert (await second.get_run(run.id)).status == "running"

    # Partition: the owner never heartbeats, never releases, never stops.
    await asyncio.sleep(_PAST_BOTH_LEASES_SECONDS)

    cancelled = await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered")
    assert [record.id for record in cancelled] == [run.id]
    assert (await second.get_run(run.id)).status == "cancelled"
    successor = await asyncio.to_thread(_acquire_as, second, run.session_id)
    fence = _fence_row(second_engine, run.session_id)
    assert fence.owner_instance_id == second.session_operation_owner_instance_id
    assert successor.fence.operation_epoch == fence.operation_epoch
    second.session_operation_authority.release(successor)

    # The dead owner's row is still ``active`` with an expired lease: a dead
    # owner, not a clean stop — the state P3's receipt distinguishes.
    row, now = _membership_row(first_engine, owner_id)
    assert row.state == InstanceState.ACTIVE.value
    assert row.lease_expires_at <= now


@pytest.mark.asyncio
async def test_heartbeating_owner_is_unstealable_until_it_stops(deployment) -> None:
    first_engine, _second_engine, first, second = deployment
    owner_id = first.session_operation_owner_instance_id
    membership = RegisteredWebInstanceMembership(
        RepositoryWebInstanceMembershipAuthority(first_engine),
        _identity(owner_id),
        lease_seconds=_SHORT_LEASE_SECONDS,
        interval_seconds=1,
    )
    await membership.start()
    try:
        run, _context = await _create_running_run(first)

        # The fence lease lapses (the owner is busy, not dead); the membership
        # lease is renewed every second, so the survivor must keep refusing.
        await asyncio.sleep(_PAST_BOTH_LEASES_SECONDS)
        assert await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered") == []
        with pytest.raises(SessionOperationConflictError):
            await asyncio.to_thread(_acquire_as, second, run.session_id)
        assert (await second.get_run(run.id)).status == "running"
        row, now = _membership_row(first_engine, owner_id)
        assert row.state == InstanceState.ACTIVE.value
        assert row.lease_expires_at > now

        await membership.begin_drain()
        assert membership.draining.is_set()
        assert _membership_row(first_engine, owner_id)[0].state == InstanceState.DRAINING.value
    finally:
        await membership.stop()

    # A clean stop expires the lease at once: no waiting out the lease.
    row, now = _membership_row(first_engine, owner_id)
    assert row.state == InstanceState.STOPPED.value
    assert row.lease_expires_at <= now
    cancelled = await second.cancel_all_orphaned_run_records(max_age_seconds=0, reason="recovered")
    assert [record.id for record in cancelled] == [run.id]
    successor = await asyncio.to_thread(_acquire_as, second, run.session_id)
    second.session_operation_authority.release(successor)


def test_register_reclaims_only_a_dead_or_stopped_incarnation(external_deployment_postgres_url: str) -> None:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    try:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity(f"membership-reclaim-{uuid4()}")
        first = authority.register(identity, lease_seconds=300)
        assert first.state is InstanceState.ACTIVE
        assert first.lease_expires_at > first.last_heartbeat_at

        from elspeth.web.coordination.membership_authority import WebInstanceRegistrationConflict

        with pytest.raises(WebInstanceRegistrationConflict):
            authority.register(identity, lease_seconds=300)

        stopped = authority.stop(identity.instance_id)
        assert stopped.state is InstanceState.STOPPED
        assert stopped.lease_expires_at == stopped.last_heartbeat_at

        reclaimed = authority.register(identity, lease_seconds=300)
        assert reclaimed.state is InstanceState.ACTIVE
        assert reclaimed.started_at >= first.started_at
    finally:
        engine.dispose()
