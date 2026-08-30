"""PostgreSQL RED gate for durable, exactly fenced composer progress."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.composer.progress import ComposerProgressRegistry, ComposerProgressSnapshot
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    session_operation_fences_table,
    web_instances_table,
)
from elspeth.web.sessions.models import metadata as sessions_metadata
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

ProgressCommitNotifier = Callable[[ComposerProgressSnapshot], Awaitable[None]]


@pytest.fixture()
def deployment(
    external_deployment_postgres_url: str,
) -> Iterator[tuple[Engine, Engine, PostgresSessionOperationRepository, PostgresSessionOperationRepository]]:
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    try:
        yield (
            first_engine,
            second_engine,
            PostgresSessionOperationRepository(first_engine),
            PostgresSessionOperationRepository(second_engine),
        )
    finally:
        first_engine.dispose()
        second_engine.dispose()


def _registry(
    engine: Engine,
    authority: PostgresSessionOperationRepository,
    *,
    notify_committed: ProgressCommitNotifier | None = None,
) -> ComposerProgressRegistry:
    signature = inspect.signature(ComposerProgressRegistry)
    for parameter_name in ("engine", "session_operation_authority"):
        assert parameter_name in signature.parameters
        assert signature.parameters[parameter_name].default is inspect.Parameter.empty
    assert "notify_committed" in signature.parameters
    assert signature.parameters["notify_committed"].default is None
    return ComposerProgressRegistry(  # type: ignore[call-arg]
        engine=engine,
        session_operation_authority=authority,
        notify_committed=notify_committed,
    )


def _create(authority: PostgresSessionOperationRepository, *, user_id: str = "alice"):
    return authority.create_session_with_initial_fence(
        user_id=user_id,
        title="PostgreSQL composer progress",
        auth_provider_type="local",
        owner_instance_id=f"progress-creator-{uuid4()}",
        lease_seconds=30,
    )


def _acquire(
    authority: PostgresSessionOperationRepository,
    *,
    session_id,
    owner: str,
) -> SessionOperationContext:
    return authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=owner,
        lease_seconds=30,
    )


def _event(phase: str, *, label: str) -> ComposerProgressEvent:
    reason = "composer_complete" if phase == "complete" else None
    return ComposerProgressEvent(
        phase=cast(Any, phase),
        headline=f"{label} {phase}",
        evidence=(f"{label} evidence",),
        likely_next=f"{label} next",
        reason=cast(Any, reason),
    )


async def _start(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str,
    event_value: ComposerProgressEvent,
) -> ComposerProgressSnapshot:
    start_request = getattr(registry, "start_request", None)
    assert callable(start_request)
    return await start_request(
        session_operation_context=context,
        request_id=request_id,
        user_id="alice",
        event=event_value,
    )


async def _publish(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str,
    event_value: ComposerProgressEvent,
) -> ComposerProgressSnapshot:
    return await registry.publish(  # type: ignore[call-arg]
        session_operation_context=context,
        request_id=request_id,
        user_id="alice",
        event=event_value,
    )


async def _finish(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str,
    event_value: ComposerProgressEvent,
) -> ComposerProgressSnapshot:
    finish_request = getattr(registry, "finish_request", None)
    assert callable(finish_request)
    return await finish_request(
        session_operation_context=context,
        request_id=request_id,
        user_id="alice",
        terminal_event=event_value,
    )


def _progress_rows(engine: Engine, *, session_id: str) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    with engine.connect() as connection:
        inflight = tuple(
            dict(row._mapping)
            for row in connection.execute(
                select(composer_inflight_requests_table)
                .where(composer_inflight_requests_table.c.session_id == session_id)
                .order_by(composer_inflight_requests_table.c.request_id)
            )
        )
        snapshots = tuple(
            dict(row._mapping)
            for row in connection.execute(
                select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == session_id)
            )
        )
    return inflight, snapshots


def _all_sessions_state(engine: Engine) -> dict[str, tuple[str, ...]]:
    """Stable value snapshot of every Sessions table, including membership and fences."""
    state: dict[str, tuple[str, ...]] = {}
    with engine.connect() as connection:
        for table in sorted(sessions_metadata.tables.values(), key=lambda candidate: candidate.name):
            rows = connection.execute(select(table)).all()
            state[table.name] = tuple(sorted(repr(dict(row._mapping)) for row in rows))
    return state


def _register_instance(engine: Engine, *, instance_id: str) -> None:
    with engine.begin() as connection:
        database_now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="composer-progress",
                session_epoch=37,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:composer-progress",
                revision_label="composer-progress",
                state="active",
                started_at=database_now,
                last_heartbeat_at=database_now,
                lease_expires_at=database_now + timedelta(minutes=5),
            )
        )


def _expire_owner(engine: Engine, *, session_id: str, instance_id: str) -> None:
    with engine.begin() as connection:
        database_now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == session_id)
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )
        connection.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == instance_id)
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )


@pytest.mark.asyncio
async def test_postgres_independent_registries_reconnect_to_committed_latest_and_liveness(deployment) -> None:
    first_engine, second_engine, first_authority, second_authority = deployment
    session = _create(first_authority)
    context = _acquire(first_authority, session_id=session.id, owner=f"owner-a-{uuid4()}")
    request_id = f"request-{uuid4()}"
    first = _registry(first_engine, first_authority)
    second = _registry(second_engine, second_authority)

    starting = await _start(first, context, request_id=request_id, event_value=_event("starting", label="start"))
    assert await second.get_latest(str(session.id)) == starting
    assert (await second.get_latest(str(session.id))).inflight_requests == 1
    published = await _publish(
        first,
        context,
        request_id=request_id,
        event_value=_event("using_tools", label="publish"),
    )
    assert await _registry(second_engine, second_authority).get_latest(str(session.id)) == published
    finished = await _finish(
        first,
        context,
        request_id=request_id,
        event_value=_event("complete", label="finish"),
    )
    assert await second.get_latest(str(session.id)) == finished
    assert await second.list_active(user_id="alice") == ()


@pytest.mark.asyncio
async def test_postgres_database_time_takeover_rejects_stale_progress_without_target_dml(deployment) -> None:
    first_engine, second_engine, first_authority, second_authority = deployment
    owner_a = f"progress-a-{uuid4()}"
    owner_b = f"progress-b-{uuid4()}"
    _register_instance(first_engine, instance_id=owner_a)
    _register_instance(first_engine, instance_id=owner_b)
    session = _create(first_authority)
    stale = _acquire(first_authority, session_id=session.id, owner=owner_a)
    stale_request = f"request-{uuid4()}"
    first = _registry(first_engine, first_authority)
    second = _registry(second_engine, second_authority)
    await _start(first, stale, request_id=stale_request, event_value=_event("starting", label="stale"))
    _expire_owner(first_engine, session_id=str(session.id), instance_id=owner_a)
    current = _acquire(second_authority, session_id=session.id, owner=owner_b)
    winner_request = f"request-{uuid4()}"
    await _start(second, current, request_id=winner_request, event_value=_event("starting", label="winner"))
    before = _progress_rows(second_engine, session_id=str(session.id))
    before_all = _all_sessions_state(second_engine)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(("insert ", "update ", "delete ")) and (
            composer_inflight_requests_table.name in normalized or composer_progress_snapshots_table.name in normalized
        ):
            statements.append(normalized)

    event.listen(first_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(SessionOperationFenceLost):
            await _publish(
                first,
                stale,
                request_id=stale_request,
                event_value=_event("saving", label="stale"),
            )
    finally:
        event.remove(first_engine, "before_cursor_execute", capture)

    assert statements == []
    assert _progress_rows(second_engine, session_id=str(session.id)) == before
    assert _all_sessions_state(second_engine) == before_all
    active = await second.list_active(user_id="alice")
    assert len(active) == 1
    assert active[0].request_id == winner_request
    assert active[0].inflight_requests == 1


@pytest.mark.asyncio
async def test_postgres_independent_threads_serialize_same_operation_latest_writes(deployment) -> None:
    first_engine, second_engine, first_authority, second_authority = deployment
    session = _create(first_authority)
    context = _acquire(first_authority, session_id=session.id, owner=f"owner-{uuid4()}")
    request_id = f"request-{uuid4()}"
    first = _registry(first_engine, first_authority)
    second = _registry(second_engine, second_authority)
    await _start(first, context, request_id=request_id, event_value=_event("starting", label="start"))
    barrier = threading.Barrier(2)

    def emit(registry: ComposerProgressRegistry, event_value: ComposerProgressEvent) -> ComposerProgressSnapshot:
        barrier.wait(timeout=5)
        return asyncio.run(
            _publish(
                registry,
                context,
                request_id=request_id,
                event_value=event_value,
            )
        )

    first_result, second_result = await asyncio.gather(
        asyncio.to_thread(emit, first, _event("using_tools", label="thread-a")),
        asyncio.to_thread(emit, second, _event("validating", label="thread-b")),
    )

    assert first_result.updated_at != second_result.updated_at
    latest = await _registry(second_engine, second_authority).get_latest(str(session.id))
    assert latest == max((first_result, second_result), key=lambda snapshot: snapshot.updated_at)
    inflight, snapshots = _progress_rows(second_engine, session_id=str(session.id))
    assert len(inflight) == 1
    assert len(snapshots) == 1
    assert snapshots[0]["operation_id"] == context.fence.operation_id
    assert snapshots[0]["operation_epoch"] == context.fence.operation_epoch
