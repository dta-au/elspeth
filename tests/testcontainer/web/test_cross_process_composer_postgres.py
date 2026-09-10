"""Composer progress custody and lifecycle proofs across spawned PostgreSQL clients."""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from typing import Annotated
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretBytes, ValidationError
from sqlalchemy import Engine, func, select, update

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.auth.models import IdentityClaims, UserIdentity
from elspeth.web.composer.progress import ComposerProgressSnapshot, ComposerRequestLease, tool_completed_progress_event
from elspeth.web.config import WebSettings
from elspeth.web.coordination.composer_progress_authority import (
    ComposerRequestLeaseLost,
    DatabaseComposerProgressRegistry,
    SessionComposerProgressAuthority,
)
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_inflight_requests_table, composer_progress_snapshots_table, identities_table
from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes.composer import guided_plan, state
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


def _http_lifecycle_process(url: str, session_id: str, user_id: str, pipe: Connection) -> None:
    """Drive a real FastAPI dependency stack in a process owning its own engine."""
    asyncio.run(_serve_lifecycle_commands(url, session_id, user_id, pipe))


async def _serve_lifecycle_commands(url: str, session_id: str, user_id: str, pipe: Connection) -> None:
    engine = create_session_engine(url)
    app = FastAPI()
    registry = DatabaseComposerProgressRegistry(SessionComposerProgressAuthority(engine, owner_instance_id=uuid4().hex, lease_seconds=2))
    app.state.session_service = SessionServiceImpl(
        engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.composer-http-pg")
    )
    app.state.settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )
    app.state.composer_progress_registry = registry
    app.dependency_overrides[_helpers.get_current_user] = lambda: UserIdentity(user_id=user_id, username="composer-pg-user")
    app.include_router(state.router)
    app.include_router(guided_plan.router)
    entered: dict[str, asyncio.Event] = {}
    requests: dict[str, asyncio.Task[Response]] = {}

    @app.post("/{session_id}/test-compose/{request_id}")
    async def compose_probe(
        session_id: UUID,
        request_id: str,
        request: Request,
        _inflight: Annotated[None, Depends(_helpers._track_compose_inflight)],
    ) -> None:
        sink = await _helpers._composer_progress_sink(registry, request, session_id=str(session_id), request_id=request_id, user_id=user_id)
        await sink(ComposerProgressEvent(phase="calling_model", headline="Waiting for the test provider"))
        # A controlled provider wait makes HTTP cancellation deterministic;
        # production lifecycle/heartbeat/publication/teardown remain intact.
        entered[request_id].set()
        await asyncio.Event().wait()

    guided_routes = [route for route in app.routes if isinstance(route, APIRoute) and route.endpoint is guided_plan.post_guided_plan]
    assert len(guided_routes) == 1
    assert any(dependency.call is _helpers._track_compose_inflight for dependency in guided_routes[0].dependant.dependencies)

    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(_helpers, "_COMPOSER_HEARTBEAT_SECONDS", 0.2)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://composer.test") as client:
                pipe.send("ready")
                while True:
                    command, argument = await asyncio.to_thread(pipe.recv)
                    if command == "stop":
                        break
                    if command == "start":
                        entered[argument] = asyncio.Event()
                        requests[argument] = asyncio.create_task(client.post(f"/{session_id}/test-compose/{argument}"))
                        await asyncio.wait_for(entered[argument].wait(), timeout=20)
                        pipe.send("provider-entered")
                    elif command == "abort":
                        task = requests.pop(argument)
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                        pipe.send("aborted")
                    elif command == "poll":
                        # A fresh HTTP client on every poll represents reload
                        # or reconnect without inheriting a prior response.
                        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://composer.test") as reader:
                            response = await reader.get(f"/{session_id}/composer-progress")
                            assert response.status_code == 200, response.text
                            pipe.send(ComposerProgressSnapshot.model_validate_json(response.content))
                    else:
                        raise AssertionError(f"Unknown lifecycle command: {command}")
                for task in requests.values():
                    task.cancel()
                await asyncio.gather(*requests.values(), return_exceptions=True)
    finally:
        engine.dispose()
        pipe.close()


@pytest.fixture()
def composer_session(external_deployment_postgres_url: str) -> Iterator[tuple[Engine, str, str]]:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    subject = f"composer-pg-{uuid4().hex}"
    identity = (
        RepositoryIdentityAuthority(engine)
        .ensure_identity(
            claims=IdentityClaims(provider="local", subject=subject, username=subject, display_name=None, email=None, organisation_id=None),
            activate=True,
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            identity_dormancy_days=90,
            record_admission=lambda *_args: None,
            record_rebound=lambda *_args: None,
            record_dormant=lambda *_args: None,
        )
        .record
    )
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.composer-pg"))
    session = asyncio.run(service.create_session(identity.identity_id, "Shared Composer", "local"))
    try:
        yield engine, str(session.id), identity.identity_id
    finally:
        engine.dispose()


def _composer_process(url: str, session_id: str, user_id: str, lease_seconds: int, pipe: Connection) -> None:
    """An independent engine and authority; pipes coordinate lifecycle boundaries only."""
    engine = create_session_engine(url)
    authority = SessionComposerProgressAuthority(engine, owner_instance_id=uuid4().hex, lease_seconds=lease_seconds)
    lease: ComposerRequestLease | None = None
    generation: str | None = None
    request_id: str | None = None
    try:
        pipe.send("ready")
        while True:
            command, argument = pipe.recv()
            if command == "stop":
                break
            try:
                if command == "begin":
                    lease = authority.begin_request(session_id, user_id)
                    pipe.send(lease.request_token)
                elif command == "bind":
                    assert lease is not None
                    request_id = argument
                    generation = authority.bind_request(lease, request_id)
                    pipe.send("bound")
                elif command == "publish":
                    assert lease is not None and generation is not None
                    authority.publish(lease, generation, request_id, argument)
                    pipe.send("published")
                elif command == "end":
                    assert lease is not None
                    authority.end_request(lease)
                    pipe.send("ended")
                elif command == "heartbeat":
                    assert lease is not None
                    authority.heartbeat_request(lease)
                    pipe.send("renewed")
                elif command == "read":
                    pipe.send(authority.get_latest(session_id, user_id))
                elif command == "active":
                    pipe.send(authority.list_active(user_id))
                elif command == "replay":
                    pipe.send(authority.replay(session_id, user_id, argument, ComposerProgressEvent(phase="complete", headline="Replayed")))
                else:
                    raise AssertionError(f"Unknown worker command: {command}")
            except (PermissionError, ComposerRequestLeaseLost, ValidationError) as exc:
                pipe.send(type(exc).__name__)
    finally:
        engine.dispose()
        pipe.close()


def _receive(pipe: Connection) -> object:
    assert pipe.poll(30), "Composer worker did not respond"
    return pipe.recv()


def _command(pipe: Connection, command: str, argument: object = None) -> object:
    pipe.send((command, argument))
    return _receive(pipe)


@contextmanager
def _workers(url: str, session_id: str, user_id: str, *, lease_seconds: int = 60) -> Iterator[tuple[Connection, Connection]]:
    context = multiprocessing.get_context("spawn")
    pairs = [context.Pipe() for _ in range(2)]
    processes = [context.Process(target=_composer_process, args=(url, session_id, user_id, lease_seconds, child)) for _, child in pairs]
    try:
        for process in processes:
            process.start()
        for parent, _ in pairs:
            assert _receive(parent) == "ready"
        yield pairs[0][0], pairs[1][0]
        for parent, _ in pairs:
            parent.send(("stop", None))
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(30)
        for parent, child in pairs:
            parent.close()
            child.close()


def _read(pipe: Connection) -> ComposerProgressSnapshot:
    result = _command(pipe, "read")
    assert isinstance(result, ComposerProgressSnapshot)
    return result


def test_fastapi_abort_heartbeat_and_reload_share_durable_lifecycle_across_processes(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    context = multiprocessing.get_context("spawn")
    pairs = [context.Pipe() for _ in range(2)]
    processes = [
        context.Process(target=_http_lifecycle_process, args=(external_deployment_postgres_url, session_id, user_id, child))
        for _, child in pairs
    ]
    try:
        for process in processes:
            process.start()
        first, second = (pair[0] for pair in pairs)
        assert _receive(first) == "ready"
        assert _receive(second) == "ready"
        assert _command(first, "start", "old-http-request") == "provider-entered"
        assert _command(second, "start", "new-http-request") == "provider-entered"
        snapshot = _command(second, "poll")
        assert isinstance(snapshot, ComposerProgressSnapshot)
        assert snapshot.inflight_requests == 2
        assert snapshot.request_id == "new-http-request"
        # No manual heartbeat: both real yield dependencies must renew while
        # their request tasks remain suspended beyond the original two seconds.
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT pg_sleep(2.5)")
        reloaded = _command(second, "poll")
        assert isinstance(reloaded, ComposerProgressSnapshot)
        assert reloaded.inflight_requests == 2
        assert _command(first, "abort", "old-http-request") == "aborted"
        after_abort = _command(second, "poll")
        assert isinstance(after_abort, ComposerProgressSnapshot)
        assert after_abort.inflight_requests == 1
        assert after_abort.request_id == "new-http-request"
        assert _command(second, "abort", "new-http-request") == "aborted"
        settled = _command(second, "poll")
        assert isinstance(settled, ComposerProgressSnapshot)
        assert settled.inflight_requests == 0
        for parent, _ in pairs:
            parent.send(("stop", None))
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(30)
        for parent, child in pairs:
            parent.close()
            child.close()


def test_spawned_requests_count_queued_work_and_preserve_latest_custody(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id) as (first, second):
        # Both admissions are released before either result is collected.
        first.send(("begin", None))
        second.send(("begin", None))
        first_token, second_token = _receive(first), _receive(second)
        assert first_token != second_token
        queued = _read(second)
        assert queued.inflight_requests == 2
        assert queued.phase == "starting"
        active = _command(second, "active")
        assert isinstance(active, tuple) and len(active) == 1
        assert active[0].session_id == session_id
        assert active[0].inflight_requests == 2
        assert _command(first, "bind", "old-request") == "bound"
        assert _command(first, "publish", ComposerProgressEvent(phase="calling_model", headline="First model call")) == "published"
        assert _read(second).headline == "First model call"
        assert _command(second, "bind", "new-request") == "bound"
        assert _command(second, "publish", ComposerProgressEvent(phase="calling_model", headline="Second model call")) == "published"
        assert _command(first, "publish", ComposerProgressEvent(phase="complete", headline="Late first completion")) == "published"
        latest = _read(first)
        assert latest.request_id == "new-request"
        assert latest.headline == "Second model call"
        assert latest.inflight_requests == 2
        assert _command(first, "end") == "ended"
        assert _command(first, "end") == "ended"
        assert _read(second).inflight_requests == 1
        with engine.connect() as conn:
            tokens = (
                conn.execute(
                    select(composer_inflight_requests_table.c.request_token).where(
                        composer_inflight_requests_table.c.session_id == session_id
                    )
                )
                .scalars()
                .all()
            )
        assert tokens == [second_token]
        assert _command(second, "end") == "ended"
        assert _read(first).inflight_requests == 0
        assert _command(first, "active") == ()


def test_bounded_global_cleanup_skips_busy_identity_and_admission_removes_abandoned_sessions(
    composer_session: tuple[Engine, str, str],
) -> None:
    engine, abandoned_id, user_id = composer_session
    authority = SessionComposerProgressAuthority(engine, owner_instance_id="cleanup-live")
    authority.cleanup_expired(limit=1000)
    short = SessionComposerProgressAuthority(engine, owner_instance_id="cleanup-short", lease_seconds=2, snapshot_ttl_seconds=2)
    leases = [short.begin_request(abandoned_id, user_id) for _ in range(5)]
    generation = short.bind_request(leases[-1], "abandoned")
    short.publish(leases[-1], generation, "abandoned", ComposerProgressEvent(phase="calling_model", headline="Abandoned work"))
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.composer-cleanup-pg"))
    live_session = asyncio.run(service.create_session(user_id, "Live work", "local"))
    live_id = str(live_session.id)
    live_lease = authority.begin_request(live_id, user_id)
    live_generation = authority.bind_request(live_lease, "live")
    authority.publish(live_lease, live_generation, "live", ComposerProgressEvent(phase="calling_model", headline="Live work"))
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT pg_sleep(2.1)")
        before = conn.execute(select(func.count()).select_from(composer_inflight_requests_table)).scalar_one()
        before += conn.execute(select(func.count()).select_from(composer_progress_snapshots_table)).scalar_one()
    assert authority.cleanup_expired(limit=2) == 2
    with engine.connect() as conn:
        after = conn.execute(select(func.count()).select_from(composer_inflight_requests_table)).scalar_one()
        after += conn.execute(select(func.count()).select_from(composer_progress_snapshots_table)).scalar_one()
        remaining = conn.execute(
            select(composer_inflight_requests_table.c.request_token).where(composer_inflight_requests_table.c.session_id == abandoned_id)
        ).all()
    assert before - after == 2
    assert len(remaining) >= 3
    # A production heartbeat/publication transaction owns this identity first.
    # Cleanup must skip that lock rather than block the request behind itself.
    with engine.begin() as conn:
        conn.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == user_id).with_for_update()).one()
        authority.cleanup_expired(limit=100)
        still_locked = conn.execute(
            select(composer_inflight_requests_table.c.request_token).where(composer_inflight_requests_table.c.session_id == abandoned_id)
        ).all()
        assert set(still_locked) == set(remaining)
    unrelated = asyncio.run(service.create_session(user_id, "Unrelated admission", "local"))
    unrelated_lease = authority.begin_request(str(unrelated.id), user_id)
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(composer_inflight_requests_table.c.request_token).where(
                    composer_inflight_requests_table.c.session_id == abandoned_id
                )
            ).all()
            == []
        )
        assert (
            conn.execute(
                select(composer_progress_snapshots_table.c.session_id).where(composer_progress_snapshots_table.c.session_id == abandoned_id)
            ).all()
            == []
        )
    live = authority.get_latest(live_id, user_id)
    assert live.inflight_requests == 1
    assert live.request_id == "live"
    assert live.headline == "Live work"
    authority.end_request(live_lease)
    authority.end_request(unrelated_lease)


def test_cleanup_preserves_expired_snapshot_custody_while_heartbeat_keeps_request_live(
    composer_session: tuple[Engine, str, str],
) -> None:
    engine, session_id, user_id = composer_session
    authority = SessionComposerProgressAuthority(engine, owner_instance_id="cleanup-heartbeat", lease_seconds=2, snapshot_ttl_seconds=2)
    lease = authority.begin_request(session_id, user_id)
    generation = authority.bind_request(lease, "long-provider")
    authority.publish(lease, generation, "long-provider", ComposerProgressEvent(phase="calling_model", headline="Before heartbeat"))
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT pg_sleep(1.2)")
    authority.heartbeat_request(lease)
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT pg_sleep(1.0)")
    authority.cleanup_expired(limit=100)
    authority.publish(lease, generation, "long-provider", ComposerProgressEvent(phase="complete", headline="After cleanup"))
    snapshot = authority.get_latest(session_id, user_id)
    assert snapshot.headline == "After cleanup"
    assert snapshot.inflight_requests == 1
    authority.end_request(lease)


def test_expired_owner_cannot_renew_or_publish_and_snapshot_survives(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id, lease_seconds=2) as (owner, observer):
        _command(owner, "begin")
        _command(owner, "bind", "expired-owner")
        _command(owner, "publish", ComposerProgressEvent(phase="calling_model", headline="Committed before partition"))
        assert _read(observer).inflight_requests == 1
        # The owner is partitioned: no heartbeat and no teardown. Database
        # time advances naturally; no production timestamps are hand-expired.
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT pg_sleep(2.1)")
        latest = _read(observer)
        assert latest.headline == "Committed before partition"
        assert latest.inflight_requests == 0
        assert _command(observer, "active") == ()
        assert _command(owner, "heartbeat") == "ComposerRequestLeaseLost"
        assert (
            _command(owner, "publish", ComposerProgressEvent(phase="complete", headline="Stale completion")) == "ComposerRequestLeaseLost"
        )
        _command(observer, "begin")
        _command(observer, "bind", "successor")
        _command(observer, "publish", ComposerProgressEvent(phase="calling_model", headline="Successor work"))
        _command(owner, "end")
        assert _read(observer).request_id == "successor"
        assert _read(observer).inflight_requests == 1
        _command(observer, "end")


def test_peer_rechecks_revocation_and_teardown_still_removes_exact_token(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id) as (owner, observer):
        _command(owner, "begin")
        _command(owner, "bind", "revoked-owner")
        _command(owner, "publish", ComposerProgressEvent(phase="calling_model", headline="Before revocation"))
        assert _read(observer).inflight_requests == 1
        with engine.begin() as conn:
            conn.execute(update(identities_table).where(identities_table.c.identity_id == user_id).values(access_state="disabled"))
        assert _command(observer, "read") == "PermissionError"
        assert _command(observer, "active") == "PermissionError"
        assert _command(observer, "begin") == "PermissionError"
        assert _command(owner, "heartbeat") == "PermissionError"
        assert _command(owner, "publish", ComposerProgressEvent(phase="complete", headline="After revocation")) == "PermissionError"
        assert _command(owner, "end") == "ended"
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(composer_inflight_requests_table.c.request_token).where(
                        composer_inflight_requests_table.c.session_id == session_id
                    )
                ).all()
                == []
            )


def test_heartbeat_keeps_long_request_live_across_initial_expiry(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id, lease_seconds=2) as (owner, observer):
        _command(owner, "begin")
        for _ in range(3):
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT pg_sleep(0.8)")
            assert _command(owner, "heartbeat") == "renewed"
        assert _read(observer).inflight_requests == 1
        _command(owner, "end")
        assert _read(observer).inflight_requests == 0


def test_killed_publisher_leaves_committed_snapshot_for_fresh_peer(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    owner = context.Process(target=_composer_process, args=(external_deployment_postgres_url, session_id, user_id, 2, child))
    try:
        owner.start()
        assert _receive(parent) == "ready"
        _command(parent, "begin")
        _command(parent, "bind", "dead-owner")
        _command(parent, "publish", ComposerProgressEvent(phase="calling_model", headline="Survives process death"))
        owner.kill()
        owner.join(30)
        assert owner.exitcode is not None and owner.exitcode < 0
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT pg_sleep(2.1)")
        # Readers start after publisher death and inherit none of its state.
        with _workers(external_deployment_postgres_url, session_id, user_id) as (first, second):
            for peer in (first, second):
                snapshot = _read(peer)
                assert snapshot.request_id == "dead-owner"
                assert snapshot.headline == "Survives process death"
                assert snapshot.inflight_requests == 0
    finally:
        if owner.is_alive():
            owner.kill()
            owner.join(30)
        parent.close()
        child.close()


def test_replay_cas_has_one_winner_and_queued_work_blocks_replay(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    _, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id) as (first, second):
        _command(first, "begin")
        assert _command(second, "replay", "stale-while-queued") is None
        _command(first, "end")
        first.send(("replay", "replay-a"))
        second.send(("replay", "replay-b"))
        results = (_receive(first), _receive(second))
        winners = [result for result in results if isinstance(result, ComposerProgressSnapshot)]
        assert len(winners) == 1
        assert sum(result is None for result in results) == 1
        assert _read(first).request_id == winners[0].request_id
        assert _read(second).request_id == winners[0].request_id


def test_peer_rejects_corrupt_snapshot_and_safe_factory_does_not_persist_raw_tool_name(
    external_deployment_postgres_url: str, composer_session: tuple[Engine, str, str]
) -> None:
    engine, session_id, user_id = composer_session
    with _workers(external_deployment_postgres_url, session_id, user_id) as (owner, observer):
        _command(owner, "begin")
        _command(owner, "bind", "safe-progress")
        private_tool_name = f"private-tool-value-{uuid4().hex}"
        _command(owner, "publish", tool_completed_progress_event(private_tool_name, success=True))
        assert _read(observer).phase == "validating"
        with engine.begin() as conn:
            persisted = conn.execute(
                select(composer_progress_snapshots_table.c.snapshot_json).where(
                    composer_progress_snapshots_table.c.session_id == session_id
                )
            ).scalar_one()
            assert private_tool_name not in persisted
            conn.execute(
                update(composer_progress_snapshots_table)
                .where(composer_progress_snapshots_table.c.session_id == session_id)
                .values(snapshot_json='{"phase":"invented-phase"}')
            )
        assert _command(observer, "read") == "ValidationError"
        assert _command(observer, "active") == "ValidationError"
        _command(owner, "end")
