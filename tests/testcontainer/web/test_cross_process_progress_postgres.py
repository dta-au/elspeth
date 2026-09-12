"""Durable progress visibility across independent PostgreSQL clients and process death."""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.websockets import WebSocketDisconnect
from tests.unit.web.execution.test_websocket import FakeBroadcaster

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.auth.models import IdentityClaims, UserIdentity
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.execution.run_progress_reader import RepositoryRunProgressReader
from elspeth.web.execution.schemas import FailedData, ProgressData, RunAccounting, RunStatusResponse
from elspeth.web.execution.websocket_ticket import WebSocketTicketStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import run_events_table
from elspeth.web.sessions.protocol import CompositionStateData, RunRecord, SessionRunEventType
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


def _noop(*_args: Any) -> None:
    return None


def _event_data(event_type: SessionRunEventType) -> dict[str, Any]:
    if event_type == "failed":
        return FailedData(detail="owner failed", node_id=None).model_dump(mode="json")
    assert event_type == "progress"
    return ProgressData(
        source_rows_processed=7,
        tokens_succeeded=5,
        tokens_failed=1,
        tokens_quarantined=1,
        tokens_routed_success=0,
        tokens_routed_failure=0,
    ).model_dump(mode="json")


def _claims(subject: str, email: str) -> IdentityClaims:
    return IdentityClaims(
        provider="vanguard",
        subject=subject,
        username=subject,
        display_name=subject,
        email=email,
        organisation_id=None,
    )


async def _seed_run(service: SessionServiceImpl, identity_id: str) -> tuple[RunRecord, SessionOperationContext]:
    session = await service.create_session(identity_id, "Cross-process progress", "vanguard")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    try:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=context,
        )
    finally:
        authority.release(context)
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    run = await service.create_run(session.id, state.id, session_operation_context=context)
    await service.update_run_status(run.id, "running", session_operation_context=context)
    return run, context


def _writer_process(url: str, identity_id: str, pipe: Connection) -> None:
    engine = create_session_engine(url)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-progress-writer"),
        owner_instance_id=f"progress-writer-{uuid4()}",
    )
    try:
        run, context = asyncio.run(_seed_run(service, identity_id))
        pipe.send((str(run.id), str(run.session_id)))
        while True:
            command = pipe.recv()
            if command is None:
                break
            event_type: SessionRunEventType = command
            record = asyncio.run(
                service.append_run_event(
                    run_id=run.id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    data=_event_data(event_type),
                    session_operation_context=context,
                )
            )
            pipe.send(record.sequence)
    finally:
        engine.dispose()
        pipe.close()


def _reader_process(url: str, identity_id: str, run_id: str, pipe: Connection) -> None:
    engine = create_session_engine(url)
    reader = RepositoryRunProgressReader(engine)
    try:
        pipe.send("ready")
        while True:
            request = pipe.recv()
            if request is None:
                break
            after_sequence, limit = request
            try:
                records = reader.read_after(identity_id=identity_id, run_id=UUID(run_id), after_sequence=after_sequence, limit=limit)
                pipe.send(None if records is None else [(row.sequence, row.event_type, dict(row.data)) for row in records])
            except AuditIntegrityError:
                pipe.send("audit-integrity-error")
    finally:
        engine.dispose()
        pipe.close()


def _receive(pipe: Connection) -> Any:
    assert pipe.poll(30), "spawned process did not answer within 30 seconds"
    return pipe.recv()


class _RouteStatusService:
    """Expose running status and signal the route's first completed empty read."""

    def __init__(self, pipe: Connection) -> None:
        self.pipe = pipe
        self.signalled_empty_read = False

    async def get_status(self, run_id: UUID, *, accounting: RunAccounting | None = None, run_record: RunRecord) -> RunStatusResponse:
        assert run_record.status == "running"
        assert accounting is None
        if not self.signalled_empty_read:
            self.pipe.send("empty-poll-completed")
            self.signalled_empty_read = True
        return RunStatusResponse(
            run_id=str(run_id),
            status="running",
            started_at=run_record.started_at,
            finished_at=None,
            error=None,
            landscape_run_id=None,
        )


def _websocket_route_process(url: str, identity_id: str, run_id: str, pipe: Connection) -> None:
    from elspeth.web.execution.routes import create_execution_router

    engine = create_session_engine(url)
    broadcaster = FakeBroadcaster()
    app = FastAPI()
    app.state.run_progress_reader = RepositoryRunProgressReader(engine)
    app.state.broadcaster = broadcaster
    app.state.execution_service = _RouteStatusService(pipe)
    app.state.session_service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-progress-route"),
        owner_instance_id=f"progress-route-{uuid4()}",
    )
    app.state.websocket_ticket_store = WebSocketTicketStore()
    app.include_router(create_execution_router())
    ticket = app.state.websocket_ticket_store.issue(run_id=run_id, user=UserIdentity(user_id=identity_id, username=identity_id)).ticket
    try:
        sent_count = 0
        with TestClient(app) as client, client.websocket_connect(f"/ws/runs/{run_id}?ticket={ticket}&after_sequence=0") as websocket:
            while True:
                try:
                    event = websocket.receive_json()
                except WebSocketDisconnect as exc:
                    close_code = exc.code
                    break
                pipe.send(event)
                sent_count += 1
        assert broadcaster.subscribe_calls == []
        assert broadcaster.unsubscribe_calls == []
        pipe.send(("closed", close_code, sent_count))
    finally:
        engine.dispose()
        pipe.close()


@contextmanager
def _process(target, args: tuple[Any, ...]) -> Iterator[tuple[multiprocessing.Process, Connection]]:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        if process.is_alive():
            process.kill()
        process.join(30)
        assert not process.is_alive(), "spawned process survived cleanup"
        parent.close()


@pytest.fixture
def progress_identity(external_deployment_postgres_url: str) -> Iterator[tuple[str, str]]:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    subject = f"progress-{uuid4()}"
    outcome = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply).ensure_identity(
        claims=_claims(subject, f"{subject}@example.com"),
        activate=True,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=90,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
    )
    try:
        yield outcome.record.identity_id, subject
    finally:
        engine.dispose()


def test_peer_events_after_subscription_and_writer_death_replay_exactly(
    external_deployment_postgres_url: str, progress_identity: tuple[str, str]
) -> None:
    url = external_deployment_postgres_url
    identity_id, _subject = progress_identity
    with _process(_writer_process, (url, identity_id)) as (writer, write):
        run_id, _session_id = _receive(write)
        with _process(_reader_process, (url, identity_id, run_id)) as (reader, read):
            assert _receive(read) == "ready"
            read.send((0, 256))
            assert _receive(read) == []  # Subscription precedes every peer write.
            for sequence, event_type in enumerate(("progress", "progress", "failed"), start=1):
                write.send(event_type)
                assert _receive(write) == sequence
            writer.kill()
            writer.join(30)
            assert writer.exitcode is not None and writer.exitcode != 0
            read.send((0, 2))
            assert _receive(read) == [(1, "progress", _event_data("progress")), (2, "progress", _event_data("progress"))]
            read.send((2, 2))
            assert _receive(read) == [(3, "failed", _event_data("failed"))]
            read.send((3, 2))
            assert _receive(read) == []
            read.send(None)
            reader.join(30)
            assert reader.exitcode == 0
        # A fresh process has no local subscriber or writer state to replay from.
        with _process(_reader_process, (url, identity_id, run_id)) as (reader, read):
            assert _receive(read) == "ready"
            read.send((1, 256))
            assert _receive(read) == [(2, "progress", _event_data("progress")), (3, "failed", _event_data("failed"))]
            read.send(None)
            reader.join(30)
            assert reader.exitcode == 0


def test_existing_reader_rechecks_identity_after_revocation(
    external_deployment_postgres_url: str, progress_identity: tuple[str, str]
) -> None:
    url = external_deployment_postgres_url
    identity_id, subject = progress_identity
    with _process(_writer_process, (url, identity_id)) as (_writer, write):
        run_id, _session_id = _receive(write)
        with _process(_reader_process, (url, identity_id, run_id)) as (reader, read):
            assert _receive(read) == "ready"
            read.send((0, 256))
            assert _receive(read) == []
            write.send("progress")
            assert _receive(write) == 1
            engine = create_session_engine(url)
            try:
                # The production rebound policy disables an identity when its
                # verified email changes; no test-only SQL revocation path.
                outcome = RepositoryIdentityAuthority(
                    engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply
                ).ensure_identity(
                    claims=_claims(subject, f"changed-{subject}@example.com"),
                    activate=False,
                    quota_tokens_per_day=None,
                    quota_storage_bytes=None,
                    identity_dormancy_days=90,
                    record_admission=_noop,
                    record_rebound=_noop,
                    record_dormant=_noop,
                )
                assert outcome.rebound_refused
                assert outcome.record.access_state == "disabled"
            finally:
                engine.dispose()
            read.send((0, 256))
            assert _receive(read) is None
            read.send(None)
            reader.join(30)
            assert reader.exitcode == 0


def test_peer_reader_refuses_persisted_sequence_gap(external_deployment_postgres_url: str, progress_identity: tuple[str, str]) -> None:
    url = external_deployment_postgres_url
    identity_id, _subject = progress_identity
    with _process(_writer_process, (url, identity_id)) as (_writer, write):
        run_id, _session_id = _receive(write)
        for sequence in (1, 2, 3):
            write.send("progress")
            assert _receive(write) == sequence
        engine = create_session_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(delete(run_events_table).where(run_events_table.c.run_id == run_id, run_events_table.c.sequence == 2))
        finally:
            engine.dispose()
        with _process(_reader_process, (url, identity_id, run_id)) as (reader, read):
            assert _receive(read) == "ready"
            read.send((1, 256))
            assert _receive(read) == "audit-integrity-error"
            read.send(None)
            reader.join(30)
            assert reader.exitcode == 0


@pytest.mark.parametrize("ending", ["revocation", "terminal"])
def test_websocket_route_streams_peer_commit_without_local_broadcast(
    external_deployment_postgres_url: str, progress_identity: tuple[str, str], ending: str
) -> None:
    url = external_deployment_postgres_url
    identity_id, subject = progress_identity
    with _process(_writer_process, (url, identity_id)) as (_writer, write):
        run_id, _session_id = _receive(write)
        with _process(_websocket_route_process, (url, identity_id, run_id)) as (reader, read):
            # Only the route's status fallback emits this barrier, after its
            # production reader completes the initial empty database poll.
            assert _receive(read) == "empty-poll-completed"
            write.send("progress")
            assert _receive(write) == 1
            event = _receive(read)
            assert event["run_id"] == run_id
            assert event["event_type"] == "progress"
            assert event["event_sequence"] == 1
            assert event["data"] == _event_data("progress")
            if ending == "terminal":
                write.send("failed")
                assert _receive(write) == 2
                event = _receive(read)
                assert event["run_id"] == run_id
                assert event["event_type"] == "failed"
                assert event["event_sequence"] == 2
                assert event["data"] == _event_data("failed")
                assert _receive(read) == ("closed", 1000, 2)
            else:
                engine = create_session_engine(url)
                try:
                    outcome = RepositoryIdentityAuthority(
                        engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply
                    ).ensure_identity(
                        claims=_claims(subject, f"changed-{subject}@example.com"),
                        activate=False,
                        quota_tokens_per_day=None,
                        quota_storage_bytes=None,
                        identity_dormancy_days=90,
                        record_admission=_noop,
                        record_rebound=_noop,
                        record_dormant=_noop,
                    )
                    assert outcome.rebound_refused
                finally:
                    engine.dispose()
                assert _receive(read) == ("closed", 4004, 1)
            reader.join(30)
            assert reader.exitcode == 0
