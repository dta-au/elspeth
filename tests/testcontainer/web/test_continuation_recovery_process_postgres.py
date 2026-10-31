"""Independent-process ownership loss and contention through actual SQL APIs."""

import asyncio
import base64
import hashlib
import json
import multiprocessing
import os
import select as fd_select
import signal
import socket
import threading
import time
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from sqlalchemy import select, text, update
from tests.testcontainer.web.test_composer_operations_postgres import _register_instance, operation_postgres_target, operation_store
from tests.unit.web.coordination.test_composer_continuation_claim_start import queued
from tests.unit.web.coordination.test_continuation_lost_owner_settlement import reconcile
from tests.unit.web.sessions.test_continuation_recovery import worker_for
from tests.unit.web.sessions.test_continuation_recovery_candidates import _started

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation_recovery_candidates import list_recoverable_segments
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import web_instances_table as instances

pytestmark = pytest.mark.testcontainer

__all__ = ["operation_postgres_target", "operation_store"]


def _wait_for_release(control, *, timeout):
    """One complete fixed pipe frame, never a shared waiter's acknowledgement."""
    if not control.poll(timeout):
        raise AssertionError("parent control was not released before its bound")
    if control.recv_bytes(maxlength=1) != b"R":
        raise AssertionError("parent control changed its exact release command")


def _recovery_process(url, candidate, name, ready, release, outcomes):
    engine = create_session_engine(url)
    try:
        ready.put(name)
        _wait_for_release(release, timeout=20)
        outcomes.put(reconcile(PostgresSessionOperationRepository(engine), candidate))
    finally:
        release.close()
        engine.dispose()


def _owner_process(url, ready, release, observations):
    engine = create_session_engine(url)
    try:
        _register_instance(engine, instance_id="process-owner", lease_delta=timedelta(minutes=10))
        authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="process-owner", claim_lease_seconds=30)
        repository = PostgresSessionOperationRepository(engine)
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(claim, owner_instance_id="process-owner", lease_seconds=30, auth_provider_type="local")
        ready.put((claim, context))
        _wait_for_release(release, timeout=60)
        try:
            authority.load_continuation_snapshot(ComposerOperationRunning(claim, context))
        except (ComposerOperationFenceLost, SessionOperationFenceLost):
            observations.put("original-owner-refused")
        else:
            observations.put("original-owner-still-authorized")
    finally:
        release.close()
        engine.dispose()


def _join_or_terminate(process):
    if process.pid is None:
        return
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=10)
    assert not process.is_alive(), "owned child was not reaped"


def _resume_stopped_child(process):
    if process.pid is None or not process.is_alive():
        return
    try:
        os.kill(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        process.join(timeout=0)
        if process.exitcode is None:
            raise


def test_two_replicas_race_recovery_one_wins_postgres(operation_store):
    engine, repository, _, _, _ = operation_store
    _, record, _, context = _started(operation_store)
    repository.release(context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    spawn = multiprocessing.get_context("spawn")
    children = []
    # Close control writers first, reap every child, then close observations.
    # Separate stacks still finish all registered cleanup if a callback fails.
    with ExitStack() as resources, ExitStack() as processes, ExitStack() as controls:
        ready, outcomes = spawn.Queue(), spawn.Queue()
        resources.callback(ready.close)
        resources.callback(outcomes.close)
        releases = []
        for name in ("recovery-left", "recovery-right"):
            receiver, sender = spawn.Pipe(duplex=False)
            resources.callback(receiver.close)
            controls.callback(sender.close)
            os.set_blocking(sender.fileno(), False)
            child = spawn.Process(
                target=_recovery_process,
                args=(engine.url.render_as_string(hide_password=False), candidate, name, ready, receiver, outcomes),
            )
            processes.callback(_join_or_terminate, child)
            children.append(child)
            releases.append(sender)
            child.start()
            receiver.close()
        assert {ready.get(timeout=30), ready.get(timeout=30)} == {"recovery-left", "recovery-right"}
        for sender in releases:
            sender.send_bytes(b"R")
        assert sorted((outcomes.get(timeout=30), outcomes.get(timeout=30))) == ["recovered", "unchanged"]
    assert all(child.exitcode == 0 for child in children)
    with engine.connect() as conn:
        history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).one()
        tags = conn.execute(select(events.c.tag).where(events.c.operation_id == record.operation_id)).scalars().all()
    assert history.revoked_at is not None and history.outcome == "interrupted"
    assert tags.count("recovered") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("zombie", (False, True), ids=("sigkill", "sigstop-sigcont"))
async def test_process_loss_requires_both_leases_and_revokes_original_writer(operation_store, tmp_path, zombie):
    engine, _, _, _, sid = operation_store
    authority, record = queued(operation_store)
    spawn = multiprocessing.get_context("spawn")
    # Cleanup never writes to a dead/stopped peer; closing the writer gives EOF.
    async with AsyncExitStack() as resources, AsyncExitStack() as processes, AsyncExitStack() as controls:
        worker, auxiliary, composer = worker_for(operation_store, tmp_path, authority)
        resources.callback(auxiliary.dispose)
        ready, observations = spawn.Queue(), spawn.Queue()
        resources.callback(ready.close)
        resources.callback(observations.close)
        receiver, sender = spawn.Pipe(duplex=False)
        resources.callback(receiver.close)
        controls.callback(sender.close)
        os.set_blocking(sender.fileno(), False)
        child = spawn.Process(target=_owner_process, args=(engine.url.render_as_string(hide_password=False), ready, receiver, observations))
        processes.push_async_callback(asyncio.to_thread, _join_or_terminate, child)
        if zombie:
            controls.callback(_resume_stopped_child, child)
        child.start()
        receiver.close()
        claim, context = await asyncio.to_thread(ready.get, timeout=30)
        assert child.is_alive() and context.fence.session_id == str(sid)
        if zombie:
            os.kill(child.pid, signal.SIGSTOP)
        else:
            child.kill()
            await asyncio.to_thread(child.join, timeout=10)
            assert child.exitcode == -signal.SIGKILL
        assert await worker.reap_once() == 0
        with engine.begin() as conn:
            assert (
                conn.execute(
                    update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                ).rowcount
                == 1
            )
        assert await worker.reap_once() == 0, "physical expiry cannot override live original membership"
        with engine.begin() as conn:
            assert (
                conn.execute(
                    update(instances)
                    .where(instances.c.instance_id == "process-owner")
                    .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                ).rowcount
                == 1
            )
        assert await worker.reap_once() == 1
        with engine.connect() as conn:
            history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).one()
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).one()
        assert history.revoked_at is not None and history.outcome == "interrupted"
        assert parent.ownership_generation == claim.ownership_generation + 1 and composer.calls == 0
        if zombie:
            os.kill(child.pid, signal.SIGCONT)
            sender.send_bytes(b"R")
            assert await asyncio.to_thread(observations.get, timeout=30) == "original-owner-refused"
    assert child.exitcode == (0 if zombie else -signal.SIGKILL)


def _process_receipt(directory, name, value):
    pending = directory / f"{name}.pending"
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(directory / f"{name}.json")


async def _hold_physical_cut():
    # The real stage/watch owner must cancel this await. A fixture timeout is a
    # failure, never a synthetic provider result or a shutdown success.
    async with asyncio.timeout(30):
        await asyncio.Future()


def _sigterm_server(session_url, landscape_url, data_dir, evidence_dir, port, mode, grace, session_id):
    """Spawn the CLI's real server/factory; script only external SDK/guard I/O."""
    import litellm
    from litellm.types.utils import ModelResponse
    from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
    from tests.helpers.composer_pg_http import pg_http_settings
    from tests.helpers.fenced_session import seed_token_policies

    import elspeth.web.app as application
    from elspeth.web.auth.local import LocalAuthProvider
    from elspeth.web.composer import prompt_guard_clients, provider_quota
    from elspeth.web.composer_server import run_composer_server
    from elspeth.web.process_watchdog import ProcessWatchdog
    from elspeth.web.sessions import continuation_driver
    from elspeth.web.sessions.composer_app_services import composer_app_services
    from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
    from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt
    from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner

    directory = Path(evidence_dir)
    with (directory / "host.log").open("a", encoding="utf-8") as output:
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
    settings = pg_http_settings(session_url, landscape_url, Path(data_dir))
    values = settings.model_dump(mode="json")
    values = {key: values[key] for key in settings.model_fields_set}
    values["shareable_link_signing_key"] = base64.b64encode(settings.shareable_link_signing_key.get_secret_value()).decode("ascii")
    guard = settings.composer_prompt_guard
    assert guard is not None
    encoded_guard = guard.model_dump(mode="json") | {"subscription_key": guard.subscription_key.get_secret_value()}
    values.update(
        composer_prompt_guard=encoded_guard,
        composer_advisor_prompt_guard=encoded_guard,
        composer_continuation_enabled=True,
        composer_async_worker_concurrency=1,
        composer_shutdown_grace_seconds=grace,
        host="127.0.0.1",
        port=port,
    )
    for key in tuple(os.environ):
        if key.startswith("ELSPETH_WEB__"):
            del os.environ[key]
    for key, value in values.items():
        os.environ[f"ELSPETH_WEB__{key.upper()}"] = value if isinstance(value, str) else json.dumps(value)
    os.environ["OPENAI_API_KEY"] = "offline-pg-http-test-only-not-a-provider-credential"
    os.environ["LITELLM_MODE"] = "PRODUCTION"
    apps, calls, claims = [], [], []
    sql_observations = []
    allowances, last_setup = {}, {}
    observation_lock = threading.Lock()
    original_cursor_execute = psycopg.Cursor.execute
    original_remaining = ContinuationSQLDeadlineOwner.remaining_statement
    original_drain_remaining = ComposerDrainDeadline.remaining_seconds
    timing_sql = "SELECT pg_catalog.set_config('lock_timeout', %s, true), pg_catalog.set_config('statement_timeout', %s, true)"
    advisory_sql = "SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))"

    def observed_remaining(owner, *, session_id, deadline=None, lifecycle=False):
        remaining = original_remaining(owner, session_id=session_id, deadline=deadline, lifecycle=lifecycle)
        allowances[threading.get_ident()] = remaining
        return remaining

    def observed_drain_remaining(deadline, *, now):
        remaining = original_drain_remaining(deadline, now=now)
        allowances[threading.get_ident()] = remaining
        return remaining

    def actual_timeouts(cursor):
        with cursor.connection.cursor() as observer:
            original_cursor_execute(
                observer,
                "SELECT current_setting('lock_timeout')::interval, current_setting('statement_timeout')::interval",
            )
            actual = observer.fetchone()
        assert actual is not None
        return [value // timedelta(milliseconds=1) for value in actual]

    def observed_execute(cursor, query, params=None, *, prepare=None, binary=None):
        app = apps[0] if apps else None
        deadline = None if app is None else app.state.composer_async_worker.drain_deadline
        started = time.monotonic()
        if query == timing_sql and deadline is not None:
            assert params is not None and len(params) == 2 and params[0] == params[1]
            requested = int(params[0].removesuffix("ms"))
            remaining = allowances.pop(threading.get_ident())
            assert remaining > 0 and requested == max(1, int(remaining * 1000))
            effective = ("0", "0") if mode == "locked-unbounded" else params
            result = original_cursor_execute(cursor, query, effective, prepare=prepare, binary=binary)
            record = {
                "kind": "timeouts",
                "pid": cursor.connection.info.backend_pid,
                "requested_ms": requested,
                "effective_ms": actual_timeouts(cursor),
            }
            with observation_lock:
                record["index"] = len(sql_observations)
                last_setup[record["pid"]] = record
                sql_observations.append(record)
                _process_receipt(directory, "sql", sql_observations)
            return result
        if query == advisory_sql and deadline is not None:
            pid = cursor.connection.info.backend_pid
            actual = actual_timeouts(cursor)
            with observation_lock:
                setup = last_setup.get(pid)
                sql_observations.append(
                    {
                        "kind": "lock",
                        "pid": pid,
                        "session_id": params[1],
                        "at": started,
                        "setup_index": None if setup is None else setup["index"],
                        "requested_ms": None if setup is None else setup["requested_ms"],
                        "effective_ms": actual,
                    }
                )
                _process_receipt(directory, "sql", sql_observations)
            try:
                return original_cursor_execute(cursor, query, params, prepare=prepare, binary=binary)
            except psycopg.Error as error:
                with observation_lock:
                    sql_observations.append({"kind": "lock-error", "pid": pid, "sqlstate": error.sqlstate})
                    _process_receipt(directory, "sql", sql_observations)
                raise
        return original_cursor_execute(cursor, query, params, prepare=prepare, binary=binary)

    def guard_response(request):
        assert str(request.url) == "https://guard.example.test/contentsafety/text:shieldPrompt?api-version=2024-09-01"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "synthetic-pg-guard-key"
        body = json.loads(request.content)
        assert set(body) == {"userPrompt", "documents"}
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    def guard_client(configuration):
        return prompt_guard_clients.AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(guard_response))

    async def offline_sdk(**kwargs):
        span = provider_quota._SPAN.get()
        assert span is not None and span.attempt is not None
        sid = span.scope.context.fence.session_id
        attempt_id = span.attempt.attempt_id
        calls.append(attempt_id)
        assert len(calls) <= 2, "unexpected physical provider replay"
        _process_receipt(directory, "sdk", {"attempt_id": attempt_id, "session_id": sid, "at": time.monotonic()})
        if mode == "peer":
            raise AssertionError("peer redispatched the retained original provider action")
        if mode == "uncertain":
            try:
                await _hold_physical_cut()
            except asyncio.CancelledError:
                _process_receipt(directory, "sdk-cancelled", {"attempt_id": attempt_id})
                raise
        return ModelResponse(
            model=kwargs["model"],
            choices=[
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "The request was explained.", "tool_calls": None}}
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    original_primary = continuation_driver.run_continuation_primary

    async def retained_primary(**kwargs):
        result = await original_primary(**kwargs)
        if mode in ("retained", "locked", "locked-unbounded"):
            _process_receipt(directory, "retained", {"at": time.monotonic()})
            await _hold_physical_cut()
        return result

    original_factory = application.create_app
    original_claim = ComposerAsyncOperationAuthority.claim_segment
    original_suspend = PostgresSessionOperationRepository.suspend_composer_segment

    def observed_suspend(repository, running, *, retry_delay_seconds=0.0, deadline=None):
        receipt = original_suspend(repository, running, retry_delay_seconds=retry_delay_seconds, deadline=deadline)
        assert type(receipt) is ComposerSegmentRecoveryReceipt
        assert receipt.released_context is running.session_operation_context
        assert receipt.lease_validity_remaining is not None and receipt.lease_validity_remaining > timedelta(0)
        fence = receipt.released_context.fence
        _process_receipt(
            directory,
            "handover",
            {
                "session_id": str(receipt.session_id),
                "operation_id": receipt.operation_id,
                "sequence": receipt.sequence,
                "previous_generation": receipt.previous_generation,
                "generation": receipt.generation,
                "checkpoint_id": receipt.checkpoint_id,
                "cursor": receipt.cursor,
                "fence_operation_id": fence.operation_id,
                "fence_epoch": fence.operation_epoch,
                "fence_lease_sha256": hashlib.sha256(fence.lease_token.encode()).hexdigest(),
                "lease_validity_seconds": receipt.lease_validity_remaining.total_seconds(),
            },
        )
        return receipt

    def observed_claim(authority, *, limit):
        started = time.monotonic()
        result = original_claim(authority, limit=limit)
        claims.append({"at": started, "claimed": [item.operation_id for item in result]})
        return result

    def observed_factory(*, drain_signal):
        app = original_factory(drain_signal=drain_signal)
        apps.append(app)
        assert app.state.composer_drain_signal is drain_signal
        assert app.state.settings.composer_continuation_enabled
        watchdog = app.state.process_watchdog
        assert type(watchdog) is ProcessWatchdog and watchdog._helper is not None
        original_lifespan = app.router.lifespan_context
        session_finalizer = app.state._session_engine_finalizer
        audit_finalizer = app.state._auth_audit_finalizer
        assert session_finalizer.alive and audit_finalizer.alive

        @asynccontextmanager
        async def observed_lifespan(actual_app):
            nonlocal session_id
            held_lock = None
            try:
                async with original_lifespan(actual_app):
                    if mode != "peer":
                        provider = app.state.auth_provider
                        assert isinstance(provider, LocalAuthProvider)
                        provider.create_user("sigterm-user", "test sigterm password", "Signal Custody")
                        with app.state.session_engine.begin() as connection:
                            ensure_test_identity(connection, identity_id="sigterm-user")
                            grant_test_pipeline_user(connection, identity_id="sigterm-user")
                            seed_token_policies(connection, identity_id="sigterm-user")
                        session = await app.state.session_service.create_session("sigterm-user", "Signal custody", "local")
                        session_id = str(session.id)
                    assert session_id is not None
                    if mode == "prestart":
                        held_lock = await composer_app_services(app).compose_locks.get_lock(session_id)
                        await held_lock.acquire()
                    _process_receipt(
                        directory,
                        "ready",
                        {
                            "session_id": session_id,
                            "instance_id": app.state.web_instance_membership.identity.instance_id,
                            "helper_pid": watchdog._helper.pid,
                        },
                    )
                    yield
                # This success path follows the ENTIRE original outer lifespan.
                # Uvicorn can swallow lifespan errors, so no finally marker counts.
                assert not session_finalizer.alive and not audit_finalizer.alive
                assert not app.state.required_generation_unavailable.is_set()
                app.state.composer_async_worker.assert_shutdown_complete()
                app.state.execution_lease_release_registry.assert_completed()
                monitor = app.state.process_recovery._monitor_task
                assert monitor is not None and monitor.done() and not monitor.cancelled() and monitor.exception() is None
                assert watchdog._disarmed and watchdog._helper.poll() == 0
                started = drain_signal.started_at
                assert started is not None and all(claim["at"] < started for claim in claims)
                actual_deadline = app.state.composer_async_worker.drain_deadline
                assert actual_deadline is not None
                assert actual_deadline.expires_at == started + app.state.settings.composer_async_drain_seconds
                _process_receipt(
                    directory,
                    "completed",
                    {
                        "session_id": session_id,
                        "instance_id": app.state.web_instance_membership.identity.instance_id,
                        "signal_started_at": started,
                        "drain_expires_at": actual_deadline.expires_at,
                        "helper_pid": watchdog._helper.pid,
                        "helper_returncode": watchdog._helper.returncode,
                        "disarmed": watchdog._disarmed,
                        "finalizers_consumed": True,
                        "monitor_joined": True,
                        "registry_completed": True,
                        "worker_completed": True,
                        "claims": claims,
                        "sdk_calls": calls,
                    },
                )
            except BaseException as error:
                _process_receipt(directory, "failed", {"type": type(error).__name__, "message": str(error)[:500]})
                raise
            finally:
                if held_lock is not None:
                    held_lock.release()

        app.router.lifespan_context = observed_lifespan
        return app

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(litellm, "acompletion", offline_sdk)
        patch.setattr(prompt_guard_clients, "build_prompt_guard_client", guard_client)
        patch.setattr(continuation_driver, "run_continuation_primary", retained_primary)
        patch.setattr(ComposerAsyncOperationAuthority, "claim_segment", observed_claim)
        patch.setattr(PostgresSessionOperationRepository, "suspend_composer_segment", observed_suspend)
        patch.setattr(ContinuationSQLDeadlineOwner, "remaining_statement", observed_remaining)
        patch.setattr(ComposerDrainDeadline, "remaining_seconds", observed_drain_remaining)
        patch.setattr(psycopg.Cursor, "execute", observed_execute)
        patch.setattr(application, "create_app", observed_factory)
        run_composer_server(uvicorn.Config("elspeth.web.app:create_app", host="127.0.0.1", port=port, factory=True, access_log=False))


def _await_process_fact(read, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        result = read()
        if result:
            return result
        assert time.monotonic() < deadline, "actual process/SQL prerequisite did not reach its bound"
        time.sleep(0.02)


def _cleanup_sigterm_host(child, helper_fds):
    try:
        _join_or_terminate(child)
    finally:
        for descriptor in helper_fds:
            try:
                if not fd_select.select([descriptor], [], [], 5)[0]:
                    # Failure cleanup is identity-bound to the observed helper.
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    assert fd_select.select([descriptor], [], [], 5)[0], "watchdog helper survived failure cleanup"
            finally:
                os.close(descriptor)


@pytest.fixture
def sigterm_hosts(composer_http_databases, aws_rds_trust_subprocess_env, tmp_path):
    from tests.testcontainer.web.test_composer_stream_replica_postgres import _provision

    databases = composer_http_databases
    _provision(databases)
    data_dir = tmp_path / "actual-app"
    for directory in (data_dir, data_dir / "blobs", data_dir / "payloads"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    engine = create_session_engine(databases.session_runtime_url)
    hosts = []
    with ExitStack() as resources:
        resources.callback(engine.dispose)

        def start(mode, grace, session_id=None):
            directory = tmp_path / f"host-{len(hosts)}"
            directory.mkdir(mode=0o700)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            child = multiprocessing.get_context("spawn").Process(
                target=_sigterm_server,
                args=(
                    databases.session_runtime_url,
                    databases.landscape_runtime_url,
                    str(data_dir),
                    str(directory),
                    port,
                    mode,
                    grace,
                    session_id,
                ),
            )
            descriptors = []
            resources.callback(_cleanup_sigterm_host, child, descriptors)
            host = {"process": child, "directory": directory, "url": f"http://127.0.0.1:{port}", "grace": grace}
            hosts.append(host)
            # The existing trust-only shim supplies the actual fixture CA to
            # spawn; it is not a production-source loader or parent import hook.
            with pytest.MonkeyPatch.context() as environment:
                for key, value in aws_rds_trust_subprocess_env.items():
                    if key == "PYTHONPATH":
                        value = os.pathsep.join((value, os.environ.get("PYTHONPATH", "")))
                    environment.setenv(key, value)
                child.start()

            def ready():
                assert child.is_alive(), (directory / "host.log").read_text(encoding="utf-8")
                if not (directory / "ready.json").exists():
                    return None
                try:
                    response = httpx.get(f"{host['url']}/api/health", timeout=0.5, trust_env=False)
                except httpx.TransportError:
                    return None
                if response.status_code != 200:
                    return None
                return json.loads((directory / "ready.json").read_text(encoding="utf-8"))

            host.update(_await_process_fact(ready))
            descriptors.append(os.pidfd_open(host["helper_pid"], 0))
            host["helper_fd"] = descriptors[0]
            with engine.connect() as connection:
                member = connection.execute(select(instances).where(instances.c.instance_id == host["instance_id"])).one()
                assert member.state == "active" and member.lease_expires_at > database_now(connection)
            return host

        yield start, engine


def _actual_operation_rows(engine, sid, operation):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.models import (
        chat_messages_table,
        composer_continuation_effects_table,
        composer_operation_segments_table,
        quota_provider_attempts_table,
        token_usage_ledger_table,
    )

    with engine.connect() as connection:
        parent = connection.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
        snapshot = load_snapshot(connection, session_id=sid, operation_id=operation)
        messages = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)).all()
        return {
            "parent": parent,
            "snapshot": decode_continuation_snapshot(snapshot.payload_json),
            "segments": connection.execute(
                select(composer_operation_segments_table)
                .where(composer_operation_segments_table.c.session_id == sid, composer_operation_segments_table.c.operation_id == operation)
                .order_by(composer_operation_segments_table.c.sequence)
            ).all(),
            "attempts": connection.execute(
                select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation).order_by(attempts.c.generation)
            ).all(),
            "effects": connection.execute(
                select(composer_continuation_effects_table)
                .where(
                    composer_continuation_effects_table.c.session_id == sid, composer_continuation_effects_table.c.operation_id == operation
                )
                .order_by(composer_continuation_effects_table.c.effect_key)
            ).all(),
            "events": connection.execute(
                select(events).where(events.c.session_id == sid, events.c.operation_id == operation).order_by(events.c.sequence)
            ).all(),
            "physical": connection.execute(
                select(quota_provider_attempts_table)
                .where(quota_provider_attempts_table.c.session_id == sid, quota_provider_attempts_table.c.source == "composer")
                .order_by(quota_provider_attempts_table.c.attempt_id)
            ).all(),
            "ledger": connection.execute(
                select(token_usage_ledger_table)
                .where(token_usage_ledger_table.c.session_id == sid, token_usage_ledger_table.c.source == "composer")
                .order_by(token_usage_ledger_table.c.entry_id)
            ).all(),
            "audit": {
                envelope["call"]["call_id"]: envelope["call"]
                for message in messages
                if message.role == "audit" and message.tool_calls
                for envelope in message.tool_calls
                if envelope.get("_kind") == "llm_call_audit"
            },
        }


def _authenticated_process_client(host):
    client = httpx.Client(base_url=host["url"], timeout=5, trust_env=False)
    try:
        login = client.post("/api/auth/login", json={"username": "sigterm-user", "password": "test sigterm password"})
        assert login.status_code == 200
        client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}", "Composer-Operation-Version": "2"})
    except BaseException:
        client.close()
        raise
    return client


def _submit_process_operation(client, host, engine, *, mode):
    from tests.helpers.composer_operations import message_body

    from elspeth.web.composer.continuation import ComposeFrame

    operation = str(uuid4())
    response = client.post(
        f"/api/sessions/{host['session_id']}/messages",
        json=message_body("Explain this request without changing the pipeline.", operation_id=operation),
    )
    assert response.status_code == 202, response.text
    assert response.headers["Composer-Operation-Version"] == "2"
    assert response.request.headers["Composer-Operation-Version"] == "2" and response.json()["operation_id"] == operation

    def cut():
        if mode != "prestart":
            marker = "sdk" if mode == "uncertain" else "retained"
            if not (host["directory"] / f"{marker}.json").exists():
                return None
        rows = _actual_operation_rows(engine, host["session_id"], operation)
        assert rows["parent"].lifecycle_version == 2
        if mode == "prestart":
            if rows["segments"][0].claim_token is not None:
                assert rows["attempts"] == [] and rows["physical"] == [] and rows["effects"] == []
                return rows
            return None
        frame = rows["snapshot"].frames[-1]
        assert type(frame) is ComposeFrame and rows["snapshot"].semantic_frontier is not None
        assert frame.phase == ("awaiting_response" if mode == "uncertain" else "response_retained")
        assert len(rows["physical"]) == 1 and len(rows["attempts"]) == 1
        (physical,) = rows["physical"]
        (effect,) = [effect for effect in rows["effects"] if effect.provider_attempt_id == physical.attempt_id]
        assert effect.disposition == ("dispatched" if mode == "uncertain" else "committed")
        if mode != "uncertain":
            assert frame.pending_response is not None and effect.result_chunk_key is not None
            assert physical.settled_at is not None and len(rows["ledger"]) == 1
            assert physical.ledger_entry_id == rows["ledger"][0].entry_id and physical.attempt_id in rows["audit"]
        return rows

    return operation, _await_process_fact(cut)


def _require_normal_exit(host, engine, origin):
    child, directory = host["process"], host["directory"]
    child.join(timeout=max(0.0, origin + host["grace"] - 5 - time.monotonic()))
    assert not child.is_alive(), "host did not exit within the original signal grace"
    elapsed = time.monotonic() - origin
    assert elapsed < host["grace"] - 5, "host exceeded the original signal grace"
    assert child.exitcode == -signal.SIGTERM, "host failed its normal graceful signal exit"
    assert not (directory / "failed.json").exists(), (
        (directory / "failed.json").read_text(encoding="utf-8") if (directory / "failed.json").exists() else ""
    )
    assert (directory / "completed.json").exists(), "host lacked successful complete outer lifespan"
    receipt = json.loads((directory / "completed.json").read_text(encoding="utf-8"))
    assert origin <= receipt["signal_started_at"] <= origin + elapsed
    assert receipt["drain_expires_at"] == receipt["signal_started_at"] + 10.0
    assert receipt["helper_pid"] == host["helper_pid"] and receipt["helper_returncode"] == 0
    assert all(
        receipt[key] is True for key in ("disarmed", "finalizers_consumed", "monitor_joined", "registry_completed", "worker_completed")
    )
    assert fd_select.select([host["helper_fd"]], [], [], 0)[0], "actual helper was not reaped before natural exit"
    with engine.connect() as connection:
        member = connection.execute(select(instances).where(instances.c.instance_id == host["instance_id"])).one()
        assert member.state == "stopped" and member.lease_expires_at <= database_now(connection)
    return receipt


def _signal_and_require_normal_exit(host, engine, *, earliest=None, latest=None):
    origin = time.monotonic()
    if earliest is not None:
        assert origin >= earliest, "signal preceded its actual held-cut premise"
    if latest is not None:
        assert origin < latest, "signal missed its original-clock pre-quantum premise"
    os.kill(host["process"].pid, signal.SIGTERM)
    return _require_normal_exit(host, engine, origin)


def _assert_actual_handover(host, engine, operation, before, original_fence):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation import read_chunk
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints

    receipt = json.loads((host["directory"] / "handover.json").read_text())
    assert receipt["session_id"] == host["session_id"] and receipt["operation_id"] == operation
    assert receipt["sequence"] == before["segments"][0].sequence
    assert receipt["previous_generation"] == before["parent"].ownership_generation
    assert receipt["generation"] == receipt["previous_generation"] + 1 and receipt["lease_validity_seconds"] > 0
    assert receipt["fence_operation_id"] == original_fence.operation_id and receipt["fence_epoch"] == original_fence.operation_epoch
    assert receipt["fence_lease_sha256"] == hashlib.sha256(original_fence.lease_token.encode()).hexdigest()
    with engine.connect() as connection:
        checkpoint = connection.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == host["session_id"],
                checkpoints.c.operation_id == operation,
                checkpoints.c.checkpoint_id == receipt["checkpoint_id"],
            )
        ).one()
        assert checkpoint.cursor == receipt["cursor"]
        recovered = connection.execute(
            select(events).where(
                events.c.session_id == host["session_id"], events.c.operation_id == operation, events.c.sequence == receipt["cursor"]
            )
        ).one()
        assert recovered.tag == "recovered"
        encoded = read_chunk(connection, session_id=host["session_id"], operation_id=operation, key=checkpoint.snapshot_chunk_key)
        root = decode_continuation_snapshot(encoded.decode("utf-8"))
        assert root.cursor == receipt["cursor"] and root.logical.session_id == host["session_id"] and root.logical.operation_id == operation
    return receipt


@pytest.mark.parametrize("grace", (30, 60), ids=("ecs30", "aca60"))
@pytest.mark.parametrize("mode", ("retained", "uncertain"))
def test_actual_sigterm_stream_full_lifespan_and_paid_cut_postgres(sigterm_hosts, mode, grace):
    start, engine = sigterm_hosts
    host = start(mode, grace)
    with _authenticated_process_client(host) as client:
        operation, before = _submit_process_operation(client, host, engine, mode=mode)
        if grace == 60:
            # Two real processes already share SQL before the signalled owner
            # exits. The ECS envelope separately starts a fresh process below.
            peer = start("peer", grace, host["session_id"])
        else:
            peer = None
        path = f"/api/sessions/{host['session_id']}/operations/{operation}"
        with client.stream("GET", f"{path}/stream") as stream:
            assert stream.status_code == 200
            first = next(line for line in stream.iter_lines() if line.startswith("data:"))
            assert json.loads(first.removeprefix("data:").strip())["operation_id"] == operation
            marker = "sdk" if mode == "uncertain" else "retained"
            held_at = json.loads((host["directory"] / f"{marker}.json").read_text())["at"]
            if grace == 30:
                # One second into this real held cut, not a fabricated segment
                # start. The concurrent-peer ACA case is pre-quantum separately.
                _await_process_fact(lambda: time.monotonic() >= held_at + 1)
            with engine.connect() as connection:
                observed_before = time.monotonic()
                now = database_now(connection)
                original_fence = connection.execute(select(fences).where(fences.c.session_id == host["session_id"])).one()
                assert original_fence.released_at is None and original_fence.lease_expires_at > now
                assert original_fence.owner_instance_id == host["instance_id"]
                original_expiry = original_fence.lease_expires_at
            segment = before["segments"][0]
            assert segment.started_at + timedelta(seconds=1) <= now
            cooperative_remaining = (segment.cooperative_at - now).total_seconds()
            assert cooperative_remaining > grace
            # Conservative test-only observation: a quantum-only drainer cannot
            # wait until this ORIGINAL SQL cooperative time and still meet G.
            latest = observed_before + cooperative_remaining - grace
            if grace == 30:
                latest = min(latest, held_at + 2)
            owner_receipt = _signal_and_require_normal_exit(host, engine, earliest=held_at + 1 if grace == 30 else None, latest=latest)
        assert len(owner_receipt["sdk_calls"]) == 1
        if mode == "retained":
            handover = _assert_actual_handover(host, engine, operation, before, original_fence)
        if peer is None:
            if mode == "retained":
                with engine.connect() as connection:
                    released = connection.execute(select(fences).where(fences.c.session_id == host["session_id"])).one()
                    assert (
                        released.operation_id == original_fence.operation_id and released.operation_epoch == original_fence.operation_epoch
                    )
                    assert released.released_at is not None
            peer = start("peer", grace, host["session_id"])
        assert peer["instance_id"] != host["instance_id"]
        with _authenticated_process_client(peer) as peer_client:
            final = _await_process_fact(
                lambda: response if (response := peer_client.get(path)).json()["status"] in ("completed", "failed") else None
            )
            after = _actual_operation_rows(engine, host["session_id"], operation)
            assert after["physical"] == before["physical"] if mode == "retained" else len(after["physical"]) == 1
            assert [row.attempt_id for row in after["physical"]] == [row.attempt_id for row in before["physical"]]
            if mode == "retained":
                assert final.json()["status"] == "completed" and after["ledger"] == before["ledger"]
                assert after["audit"] == before["audit"]
                assert len(after["segments"]) == 1 and after["segments"][0].sequence == before["segments"][0].sequence
                assert after["parent"].ownership_generation == before["parent"].ownership_generation + 1
                original, successor = after["attempts"]
                assert original.outcome == "interrupted" and original.revoked_at is not None
                assert successor.owner_instance_id == peer["instance_id"] and successor.started_at < original_expiry
                assert successor.generation == original.generation + 1
                assert successor.generation == handover["generation"]
                assert [event.tag for event in after["events"]].count("recovered") == 1
            else:
                assert final.json()["status"] == "failed" and after["parent"].failure_code == "worker_lost"
                assert (host["directory"] / "sdk-cancelled.json").exists()
                (physical,) = after["physical"]
                assert len(after["ledger"]) == 1
                assert physical.settled_at is not None and physical.ledger_entry_id == after["ledger"][0].entry_id
                assert after["ledger"][0].prompt_tokens is None and after["ledger"][0].completion_tokens is None
                assert physical.attempt_id in after["audit"]
                assert [effect.disposition for effect in after["effects"] if effect.provider_attempt_id == physical.attempt_id] == [
                    "unknown"
                ]
                assert [event.tag for event in after["events"]].count("effect_unknown") == 1
                assert len(after["attempts"]) == 1
            assert not (peer["directory"] / "sdk.json").exists(), "reconnect/resume re-entered the paid action"
            assert _signal_and_require_normal_exit(peer, engine)["sdk_calls"] == []


@pytest.mark.parametrize("grace", (30, 60), ids=("ecs30", "aca60"))
def test_actual_sigterm_releases_prestart_and_claims_nothing_later_postgres(sigterm_hosts, grace):
    start, engine = sigterm_hosts
    host = start("prestart", grace)
    with _authenticated_process_client(host) as client:
        operation, before = _submit_process_operation(client, host, engine, mode="prestart")
        created = client.post("/api/sessions", json={"title": "Queued signal witness"})
        assert created.status_code == 201
        sid = created.json()["id"]
        later = str(uuid4())
        from tests.helpers.composer_operations import message_body

        admitted = client.post(
            f"/api/sessions/{sid}/messages", json=message_body("Explain without changing the pipeline.", operation_id=later)
        )
        assert admitted.status_code == 202
        later_before = _actual_operation_rows(engine, sid, later)
        assert later_before["segments"][0].claim_token is None and later_before["attempts"] == []
        receipt = _signal_and_require_normal_exit(host, engine)
        for current_sid, current_operation, initial in ((host["session_id"], operation, before), (sid, later, later_before)):
            rows = _actual_operation_rows(engine, current_sid, current_operation)
            assert rows["parent"].status == "queued" and rows["parent"].started_at is None
            assert rows["parent"].ownership_generation == initial["parent"].ownership_generation
            assert rows["segments"][0].claim_token is None
            assert rows["attempts"] == rows["physical"] == rows["effects"] == rows["ledger"] == []
        assert receipt["sdk_calls"] == [] and not (host["directory"] / "sdk.json").exists()


@pytest.mark.parametrize("unbounded", (False, True), ids=("both-deadlines", "both-omitted-opponent"))
def test_actual_sigterm_physical_lock_deadline_opposition_postgres(sigterm_hosts, unbounded):
    from elspeth.web.sessions.locking import locked_session_transaction

    start, engine = sigterm_hosts
    host = start("locked-unbounded" if unbounded else "locked", 30)
    with _authenticated_process_client(host) as client:
        operation, before = _submit_process_operation(client, host, engine, mode="retained")
        path = f"/api/sessions/{host['session_id']}/operations/{operation}"
        # Establish the actual TCP reader before taking the external SQL lock.
        # Otherwise the initial stream read itself could block before SIGTERM.
        with client.stream("GET", f"{path}/stream") as stream:
            assert stream.status_code == 200
            first = next(line for line in stream.iter_lines() if line.startswith("data:"))
            assert json.loads(first.removeprefix("data:").strip())["operation_id"] == operation
            with locked_session_transaction(engine, host["session_id"]) as holder:
                holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()

                def waiter():
                    evidence = host["directory"] / "sql.json"
                    if not evidence.exists():
                        return None
                    observations = json.loads(evidence.read_text())
                    candidates = [item for item in observations if item["kind"] == "lock" and item["session_id"] == host["session_id"]]
                    with engine.connect() as connection:
                        for candidate in reversed(candidates):
                            waiting = connection.execute(
                                text(
                                    "SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid WHERE a.pid = :pid "
                                    "AND a.wait_event_type = 'Lock' AND l.locktype = 'advisory' AND NOT l.granted "
                                    "AND :holder = ANY(pg_blocking_pids(a.pid))"
                                ),
                                {"pid": candidate["pid"], "holder": holder_pid},
                            ).first()
                            if waiting is not None:
                                assert candidate["setup_index"] is not None and candidate["requested_ms"] is not None
                                configured = observations[candidate["setup_index"]]
                                assert configured["kind"] == "timeouts" and configured["pid"] == candidate["pid"]
                                assert configured["requested_ms"] == candidate["requested_ms"] > 0
                                expected = [0, 0] if unbounded else [candidate["requested_ms"]] * 2
                                assert configured["effective_ms"] == candidate["effective_ms"] == expected
                                return candidate
                    return None

                origin = time.monotonic()
                os.kill(host["process"].pid, signal.SIGTERM)
                waiting = _await_process_fact(waiter, timeout=10)
                # Reuse the complete original-origin oracle without another
                # signal. The opponent must fail that SAME grace check.
                child = host["process"]
                if unbounded:
                    with pytest.raises(AssertionError, match="host did not exit within the original signal grace"):
                        _require_normal_exit(host, engine, origin)
                    assert child.is_alive() and not (host["directory"] / "completed.json").exists()
                    child.kill()
                    child.join(timeout=10)
                    assert child.exitcode == -signal.SIGKILL
                else:
                    _require_normal_exit(host, engine, origin)
                    finished_sql = json.loads((host["directory"] / "sql.json").read_text())
                    assert any(
                        item["kind"] == "lock-error" and item["pid"] == waiting["pid"] and item["sqlstate"] in ("55P03", "57014")
                        for item in finished_sql
                    )
                unchanged = _actual_operation_rows(engine, host["session_id"], operation)
                for key in ("parent", "segments", "attempts", "effects", "events", "physical", "ledger", "audit", "snapshot"):
                    assert unchanged[key] == before[key], f"deadline left a partial {key} cut"
        # Both branches leave the genuine complete retained result for ordinary
        # peer recovery. No lease is forged/expired or timeout/grace enlarged.
        peer = start("peer", 30, host["session_id"])
        with _authenticated_process_client(peer) as peer_client:
            path = f"/api/sessions/{host['session_id']}/operations/{operation}"
            _await_process_fact(lambda: peer_client.get(path).json()["status"] == "completed", timeout=40)
            after = _actual_operation_rows(engine, host["session_id"], operation)
            assert after["physical"] == before["physical"] and after["ledger"] == before["ledger"] and after["audit"] == before["audit"]
            assert len(after["segments"]) == 1 and [event.tag for event in after["events"]].count("recovered") == 1
            assert _signal_and_require_normal_exit(peer, engine)["sdk_calls"] == []
