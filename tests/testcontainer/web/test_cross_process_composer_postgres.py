"""Composer progress custody and lifecycle proofs across spawned PostgreSQL clients."""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import structlog
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.exc import ProgrammingError
from tests.fixtures.identities import grant_test_pipeline_user
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_pg_http import mounted_pg_http_process, pg_http_settings
from tests.testcontainer.web.test_external_deployment_postgres import _DatabasePair, _identifier

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.composer.progress import ComposerProgressSnapshot, ComposerRequestLease, tool_completed_progress_event
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.composer_progress_authority import (
    ComposerRequestLeaseLost,
    SessionComposerProgressAuthority,
)
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.schema_probe import SchemaState, init_landscape_schema, init_session_schema, probe_landscape_schema, probe_session_schema
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    identities_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture()
def composer_session(external_deployment_postgres_url: str) -> Iterator[tuple[Engine, str, str]]:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    subject = f"composer-pg-{uuid4().hex}"
    identity = (
        RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
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


@pytest.fixture
def composer_http_databases(external_deployment_postgres_url: str) -> Iterator[_DatabasePair]:
    """Separate task-owned databases; owner bootstrap and DDL-denied runtime role."""
    databases = _DatabasePair(
        postgres_url=external_deployment_postgres_url,
        session_database=_identifier("composer_http_session"),
        landscape_database=_identifier("composer_http_landscape"),
        runtime_role=_identifier("composer_http_runtime"),
        runtime_password=uuid4().hex,
    )
    assert databases.session_database != databases.landscape_database
    admin = create_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            for name in (databases.session_database, databases.landscape_database):
                connection.exec_driver_sql(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)).as_string())
        yield databases
    finally:
        with admin.connect() as connection:
            for name in (databases.session_database, databases.landscape_database):
                connection.exec_driver_sql(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)).as_string())
            if databases.role_created:
                connection.exec_driver_sql(sql.SQL("DROP ROLE {}").format(sql.Identifier(databases.runtime_role)).as_string())
        admin.dispose()


def test_fastapi_abort_heartbeat_and_reload_share_durable_lifecycle_across_processes(
    composer_http_databases: _DatabasePair, tmp_path: Path
) -> None:
    # Historical abort now means observer detach. User Stop is a separate actor.
    # Both peers mount the production app; only the external SDK is offline.
    databases = composer_http_databases
    session_owner = create_session_engine(databases.session_owner_url)
    landscape_owner = create_engine(databases.landscape_owner_url)
    try:
        init_session_schema(session_owner)
        init_landscape_schema(landscape_owner)
        assert probe_session_schema(session_owner) is SchemaState.CURRENT
        assert probe_landscape_schema(landscape_owner) is SchemaState.CURRENT
        # Same explicit role topology as the external-state acceptance helper:
        # fresh schema-owner bootstrap, then a non-owner LOGIN role.
        databases.provision_runtime_role()
        for owner, permissions in (
            (session_owner, "SELECT, INSERT, UPDATE, DELETE"),
            (landscape_owner, "SELECT, INSERT, UPDATE"),
        ):
            with owner.begin() as connection:
                connection.exec_driver_sql(
                    sql.SQL("GRANT " + permissions + " ON ALL TABLES IN SCHEMA public TO {}")
                    .format(sql.Identifier(databases.runtime_role))
                    .as_string()
                )
                connection.exec_driver_sql(
                    sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                    .format(sql.Identifier(databases.runtime_role))
                    .as_string()
                )
    finally:
        session_owner.dispose()
        landscape_owner.dispose()
    for url in (databases.session_runtime_url, databases.landscape_runtime_url):
        runtime = create_engine(url)
        try:
            with runtime.connect() as connection:
                identity = connection.execute(
                    text("SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")
                ).one()
                assert identity == (databases.runtime_role, False, False, False)
                assert connection.execute(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")).scalar_one() is False
            with pytest.raises(ProgrammingError) as refused, runtime.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE composer_http_runtime_ddl_must_fail (id integer)")
            assert isinstance(refused.value.orig, psycopg.errors.InsufficientPrivilege)
            assert refused.value.orig.sqlstate == "42501"
        finally:
            runtime.dispose()
    for directory in (tmp_path, tmp_path / "blobs", tmp_path / "payloads"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    async def prepare() -> tuple[str, str, str]:
        with pytest.MonkeyPatch.context() as credential_patch:
            credential_patch.setenv("OPENAI_API_KEY", "offline-pg-http-test-only-not-a-provider-credential")
            app = create_app(
                settings=pg_http_settings(databases.session_runtime_url, databases.landscape_runtime_url, tmp_path),
                process_watchdog_factory=OwnedTestProcessWatchdog,
            )
            async with app.router.lifespan_context(app):
                provider = app.state.auth_provider
                assert isinstance(provider, LocalAuthProvider)
                username = f"http-custody-{uuid4().hex}"
                provider.create_user(username, "test mounted composer password", "HTTP Custody")
                token = await provider.login(username, "test mounted composer password")
                principal = await provider.authenticate(token)
                with app.state.session_engine.begin() as connection:
                    grant_test_pipeline_user(connection, identity_id=principal.user_id)
                session = await app.state.session_service.create_session(principal.user_id, "Shared durable HTTP Composer", "local")
                return str(session.id), principal.user_id, username

    session_id, user_id, username = asyncio.run(prepare())
    engine = create_session_engine(databases.session_runtime_url)
    operation_id = str(uuid4())
    context = multiprocessing.get_context("spawn")
    pairs = [context.Pipe() for _ in range(2)]
    processes = [
        context.Process(
            target=mounted_pg_http_process,
            args=(databases.session_runtime_url, databases.landscape_runtime_url, str(tmp_path), session_id, user_id, username, child),
        )
        for _, child in pairs
    ]
    try:
        for process in processes:
            process.start()
        first, second = (pair[0] for pair in pairs)
        assert _receive(first) == "ready"
        assert _receive(second) == "ready"
        admitted = _command(first, "start", operation_id)
        assert admitted["accepted"]["operation_id"] == operation_id
        assert admitted["sdk_calls"] == 1
        own_observation = _command(first, "observe", operation_id)
        peer_observation = _command(second, "observe", operation_id)
        assert own_observation == {"observed": operation_id, "permits": 1, "sdk_calls": 1}
        assert peer_observation == {"observed": operation_id, "permits": 1, "sdk_calls": 0}
        snapshot = _command(second, "poll", operation_id)
        assert snapshot["snapshot"]["operation_id"] == operation_id
        assert snapshot["snapshot"]["status"] == "running"
        assert snapshot["attempt"] == 1
        assert snapshot["cancel_requested_at"] is None
        assert snapshot["claim_expires_at"] is None
        initial_fence = snapshot["fence"]
        assert initial_fence["operation_kind"] == "compose"
        assert initial_fence["owner_instance_id"] == snapshot["claim_owner"]
        assert initial_fence["released_at"] is None
        assert initial_fence["lease_expires_at"] > snapshot["fence_database_now"]
        # Production SessionService uses its30-second default; WebSettings
        # does not expose a shorter session TTL. Advance real PostgreSQL time
        # past the actual originally observed session fence expiry.
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT pg_sleep(31.0)")
        reloaded = _command(second, "poll", operation_id)
        assert reloaded["snapshot"]["status"] == "running"
        assert reloaded["claim_owner"] == snapshot["claim_owner"]
        assert reloaded["attempt"] == 1
        assert reloaded["claim_expires_at"] is None
        renewed_fence = reloaded["fence"]
        assert reloaded["fence_database_now"] > initial_fence["lease_expires_at"]
        assert renewed_fence["operation_id"] == initial_fence["operation_id"]
        assert renewed_fence["operation_epoch"] == initial_fence["operation_epoch"]
        assert renewed_fence["owner_instance_id"] == initial_fence["owner_instance_id"]
        assert renewed_fence["released_at"] is None
        assert renewed_fence["lease_expires_at"] > reloaded["fence_database_now"]
        assert renewed_fence["lease_expires_at"] > initial_fence["lease_expires_at"]
        assert reloaded["sdk_calls"] == 0
        assert reloaded["permits"] == 1
        detached = _command(first, "disconnect", operation_id)
        assert detached == {"detached": operation_id, "permits": 0, "sdk_calls": 1, "sdk_cancelled": 0}
        after_abort = _command(second, "poll", operation_id)
        assert after_abort["snapshot"]["status"] == "running"
        assert after_abort["cancel_requested_at"] is None
        assert after_abort["permits"] == 1
        assert after_abort["attempt"] == 1
        stopped = _command(first, "cancel", operation_id)
        assert stopped["terminal"]["operation_id"] == operation_id
        assert stopped["terminal"]["status"] == "failed"
        assert stopped["terminal"]["error"]["http_status"] == 499
        assert stopped["sdk_calls"] == 1
        assert stopped["sdk_cancelled"] == 1
        observed_terminal = _command(second, "finish-observer", operation_id)
        assert observed_terminal == {"terminal_observed": operation_id, "permits": 0, "sdk_calls": 0}
        settled = _command(second, "poll", operation_id)
        assert settled["snapshot"] == stopped["terminal"]
        assert settled["cancel_requested_at"] is not None
        assert settled["attempt"] == 1
        assert settled["sdk_calls"] == 0
        with engine.connect() as connection:
            roles = (
                connection.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == session_id)).scalars().all()
            )
        assert roles.count("user") == 1
        assert roles.count("assistant") == 0
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
        engine.dispose()


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
    # An identity administration transaction can own this identity independently.
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
        # The precise class, not the `PermissionError` base: only the identity
        # was disabled, and `_ownership_query` reads `sessions` alone, so the
        # session guard still passes on every one of these paths. Naming the
        # subclass proves the identity check is what refused — the base class
        # would accept a session-unavailable answer as though it were the same
        # denial, which is exactly the conflation the typed hierarchy removes.
        assert _command(observer, "read") == "ComposerProgressIdentityInactive"
        assert _command(observer, "active") == "ComposerProgressIdentityInactive"
        assert _command(observer, "begin") == "ComposerProgressIdentityInactive"
        assert _command(owner, "heartbeat") == "ComposerProgressIdentityInactive"
        assert (
            _command(owner, "publish", ComposerProgressEvent(phase="complete", headline="After revocation"))
            == "ComposerProgressIdentityInactive"
        )
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
