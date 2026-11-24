"""PostgreSQL: a durable composer turn whose session lease lapses settles, and the session accepts the next turn.

The app is built by ``create_app`` with production pool settings and runs its
own composer worker. Only the external model SDK and the prompt-guard HTTP
transport are scripted. A turn is admitted over HTTP and blocks inside the
model call; every main-pool connection is then held for longer than the turn's
session lease, so its renewal reaches the database after the deadline. The
turn ends with its failed terminal refused before any write, the worker's
recovery sweep settles the job and releases that fence in one transaction, and
the same live replica starts and completes the next turn on the session.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from psycopg import sql
from sqlalchemy import Connection, Engine, create_engine, func, select, update
from sqlalchemy.exc import OperationalError
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.composer_operations import message_body
from tests.testcontainer.web.test_external_deployment_postgres import _DatabasePair, _identifier
from tests.testcontainer.web.test_lease_upkeep_pool_postgres import _build_app, _prepare

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.process_watchdog_codec import RecoveryReason
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import RequiredWorkIncomplete, RequiredWorkSource
from elspeth.web.schema_probe import postgres_engine_kwargs
from elspeth.web.sessions.composer_operation_errors import worker_lost_error
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRecord
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table

pytestmark = pytest.mark.testcontainer

_LEASE_SECONDS = 3
_HOLD_SECONDS = 6.0  # longer than the lease, shorter than the 30 s pool_timeout
_DEFAULT_LEASE_SECONDS = 30


@pytest.fixture
def databases(external_deployment_postgres_url: str) -> Iterator[_DatabasePair]:
    pair = _DatabasePair(
        postgres_url=external_deployment_postgres_url,
        session_database=_identifier("lapsed_turn_session"),
        landscape_database=_identifier("lapsed_turn_landscape"),
        runtime_role=_identifier("lapsed_turn_runtime"),
        runtime_password=uuid4().hex,
    )
    admin = create_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            for name in (pair.session_database, pair.landscape_database):
                connection.exec_driver_sql(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)).as_string())
        yield pair
    finally:
        with admin.connect() as connection:
            for name in (pair.session_database, pair.landscape_database):
                connection.exec_driver_sql(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)).as_string())
            if pair.role_created:
                connection.exec_driver_sql(sql.SQL("DROP ROLE {}").format(sql.Identifier(pair.runtime_role)).as_string())
        admin.dispose()


@pytest.fixture
def observer(databases: _DatabasePair) -> Iterator[Engine]:
    engine = create_session_engine(databases.session_owner_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _fence(engine: Engine, session_id: UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id))
        ).one()
        return {
            "operation_id": row.operation_id,
            "operation_epoch": row.operation_epoch,
            "lease_token": row.lease_token,
            "operation_kind": row.operation_kind,
            "released": row.released_at is not None,
            "expired": row.lease_expires_at <= now,
        }


def _member_live(engine: Engine, instance_id: str) -> bool:
    with engine.connect() as conn:
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        expiry: datetime = conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == instance_id)
        ).scalar_one()
        return bool(expiry > now)


def _leaves(error: BaseException | None) -> list[type[BaseException]]:
    """Every non-group member of an escaping failure, and each direct cause, depth first."""
    found: list[type[BaseException]] = []
    pending: list[BaseException] = [] if error is None else [error]
    seen: list[BaseException] = []
    while pending:
        current = pending.pop()
        if any(current is earlier for earlier in seen):
            continue
        seen.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
        else:
            found.append(type(current))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return found


class _OfflineModel:
    """The external model SDK: each call blocks until released, then answers."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = 0

    async def acompletion(self, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return ModelResponse(
            choices=[Choices(message=Message(content="Offline provider completed.", role="assistant"), finish_reason="stop")],
            model=kwargs["model"],
            id="offline-lapsed-turn-response",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def _guard_response(request: httpx.Request) -> httpx.Response:
    documents = json.loads(request.content)["documents"]
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False} for _ in documents],
        },
    )


def _guard_client(settings: PromptGuardSettings) -> prompt_guard_clients.AzurePromptGuardClient:
    return prompt_guard_clients.AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(_guard_response))


async def _wait_for(predicate: Any, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


async def _poll_terminal(app: FastAPI, session_id: UUID, operation_id: str, *, seconds: float) -> ComposerOperationRecord | None:
    authority = app.state.composer_async_operation_authority
    deadline = time.monotonic() + seconds
    while True:
        record: ComposerOperationRecord | None = authority.get(session_id=session_id, operation_id=operation_id)
        if record is not None and record.status in ("completed", "failed"):
            return record
        if time.monotonic() >= deadline:
            return record
        await asyncio.sleep(0.1)


def _job_fields(record: ComposerOperationRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "status": record.status,
        "settled_by": record.settled_by,
        "failure_code": record.failure_code,
        "session_operation_id": record.session_operation_id,
        "session_operation_epoch": record.session_operation_epoch,
    }


def _run(
    databases: _DatabasePair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observer: Engine,
    *,
    hold_pool: bool,
    probe: Literal["none", "foreign_owner", "flagless_own", "peer_settles", "commit_then_error"] = "none",
) -> dict[str, Any]:
    _prepare(databases, tmp_path, monkeypatch)
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", _guard_client)
    model = _OfflineModel()
    monkeypatch.setattr(litellm, "acompletion", model.acompletion)
    adopted: list[SessionOperationLease] = []
    original_adopt = SessionOperationLease.adopt

    async def recording_adopt(*args: Any, **kwargs: Any) -> SessionOperationLease:
        lease = await original_adopt(*args, **kwargs)
        adopted.append(lease)
        return lease

    monkeypatch.setattr(SessionOperationLease, "adopt", recording_adopt)
    pool_kwargs = postgres_engine_kwargs(databases.session_runtime_url)
    main_capacity = pool_kwargs["pool_size"] + pool_kwargs["max_overflow"]
    record: dict[str, Any] = {}
    peer: dict[str, Any] = {}

    async def body(app: FastAPI) -> None:
        session_service = app.state.session_service
        instance_id = session_service.session_operation_owner_instance_id
        worker = app.state.composer_async_worker
        provider = app.state.auth_provider
        assert type(provider) is LocalAuthProvider
        username = f"lapsed-turn-{uuid4().hex}"
        provider.create_user(username, "test lapsed turn password", "Lapsed Turn")
        token = await provider.login(username, "test lapsed turn password")
        principal = await provider.authenticate(token)
        with app.state.session_engine.begin() as conn:
            grant_test_pipeline_user(conn, identity_id=principal.user_id)
        session = await session_service.create_session(principal.user_id, "Lapsed turn", "local")
        session_id = UUID(str(session.id))
        # The turn's session lease: short enough for a pool hold to outlast it.
        monkeypatch.setattr(session_service, "_session_operation_lease_seconds", _LEASE_SECONDS)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://composer.test", headers={"Authorization": f"Bearer {token}"}
        ) as client:
            first = message_body("Explain this request without changing the pipeline.")
            accepted = await client.post(f"/api/sessions/{session_id}/messages", json=first)
            assert accepted.status_code == 202, accepted.text
            key = (session_id, first["operation_id"])
            await asyncio.wait_for(model.entered.wait(), timeout=30)
            task = worker._jobs[key]
            coordinator = worker._required_coordinators[key]
            turn_fence = _fence(observer, session_id)
            record["turn_fence"] = {name: turn_fence[name] for name in ("operation_epoch", "released", "expired")}

            def at_turn_end(finished: asyncio.Task[Any]) -> None:
                # Runs after the worker's own done callback, before its loop wakes.
                failure = None if finished.cancelled() else finished.exception()
                fence = _fence(observer, session_id)
                record["at_turn_end"] = {
                    "task_leaves": _leaves(failure),
                    "fence_same_identity": all(
                        fence[name] == turn_fence[name] for name in ("operation_id", "lease_token", "operation_epoch")
                    ),
                    "fence_released": fence["released"],
                    "fence_expired": fence["expired"],
                    "in_jobs": key in worker._jobs,
                    "in_adoption_recovery_pending": key in worker._adoption_recovery_pending,
                    "unsettled_owner": worker._unsettled_owners.get(key),
                    "coordinator_retained": key in worker._required_coordinators,
                    "coordinator_all_completed": coordinator.all_completed,
                    "incomplete_sources": [ticket.key.source for ticket in coordinator.tickets if not ticket.complete],
                    "instance_draining": app.state.instance_draining.is_set(),
                }
                if probe == "foreign_owner":
                    # Another instance's settle_own_lapsed, asked to release, on this
                    # instance's lapsed job: the owner predicate must refuse it.
                    foreign_id = f"foreign-{uuid4().hex}"
                    foreign = ComposerAsyncOperationAuthority(
                        app.state.session_engine, owner_instance_id=foreign_id, claim_lease_seconds=30
                    )
                    raised: type[BaseException] | None = None
                    try:
                        foreign.settle_own_lapsed(
                            session_id=session_id,
                            operation_id=first["operation_id"],
                            owner_instance_id=foreign_id,
                            failure=worker_lost_error(request_id=None),
                            release_lapsed_fence=True,
                        )
                    except ComposerOperationFenceLost as refused:
                        raised = type(refused)
                    record["foreign"] = {
                        "raised": raised,
                        "fence_after": _fence(observer, session_id),
                        "job_status": app.state.composer_async_operation_authority.get(
                            session_id=session_id, operation_id=first["operation_id"]
                        ).status,
                    }
                elif probe == "flagless_own":
                    # The in-process caller's shape: this instance's own settlement
                    # without the reap-only flag settles the job and keeps the fence.
                    settled_here = app.state.composer_async_operation_authority.settle_own_lapsed(
                        session_id=session_id,
                        operation_id=first["operation_id"],
                        owner_instance_id=instance_id,
                        failure=worker_lost_error(request_id=None),
                    )
                    record["flagless"] = {"job": _job_fields(settled_here), "fence_after": _fence(observer, session_id)}
                elif probe == "peer_settles":
                    # A peer takes the session over once this instance's membership has
                    # lapsed, and settles the job under its own live fence before this
                    # instance's reaper reaches it.
                    composer_authority = app.state.composer_async_operation_authority
                    stale = composer_authority.get(session_id=session_id, operation_id=first["operation_id"])
                    peer_id = f"peer-{uuid4().hex}"
                    with observer.begin() as conn:
                        now = conn.execute(select(func.clock_timestamp())).scalar_one()
                        conn.execute(
                            update(web_instances_table)
                            .where(web_instances_table.c.instance_id == instance_id)
                            .values(lease_expires_at=now - timedelta(seconds=1))
                        )
                    try:
                        peer_context = session_service.session_operation_authority.acquire(
                            session_id=session_id,
                            operation_kind=SessionOperationKind.COMPOSE,
                            owner_instance_id=peer_id,
                            lease_seconds=_DEFAULT_LEASE_SECONDS,
                        )
                    finally:
                        with observer.begin() as conn:
                            now = conn.execute(select(func.clock_timestamp())).scalar_one()
                            conn.execute(
                                update(web_instances_table)
                                .where(web_instances_table.c.instance_id == instance_id)
                                .values(lease_expires_at=now + timedelta(seconds=60))
                            )
                    peer_authority = ComposerAsyncOperationAuthority(
                        app.state.session_engine, owner_instance_id=peer_id, claim_lease_seconds=30
                    )
                    peer_settled = peer_authority.settle_lost(
                        session_operation_context=peer_context,
                        session_id=session_id,
                        operation_id=first["operation_id"],
                        failure=worker_lost_error(request_id=None),
                    )
                    peer["context"] = peer_context
                    peer["stale"] = stale
                    original_list = composer_authority.list_expired_running

                    def listed_before_the_peer_committed(*, limit: int) -> tuple[ComposerOperationRecord, ...]:
                        # The reaper's listing raced the peer: it still sees the job running.
                        if "stale" in peer:
                            return (peer["stale"],)
                        return original_list(limit=limit)

                    monkeypatch.setattr(composer_authority, "list_expired_running", listed_before_the_peer_committed)
                    record["peer"] = {"settled_by": peer_settled.settled_by, "fence_after_peer": _fence(observer, session_id)}
                elif probe == "commit_then_error":
                    # The reaper's own-lapsed settlement commits on PostgreSQL,
                    # then its caller sees an OperationalError.
                    composer_authority = app.state.composer_async_operation_authority
                    actual_settle_own_lapsed = composer_authority.settle_own_lapsed
                    record["committed_then_failed"] = []

                    def settle_then_fail(**kwargs: Any) -> ComposerOperationRecord:
                        settled_here = actual_settle_own_lapsed(**kwargs)
                        record["committed_then_failed"].append(settled_here.settled_by)
                        raise OperationalError("settle_own_lapsed", {}, RuntimeError("connection lost after commit"))

                    monkeypatch.setattr(composer_authority, "settle_own_lapsed", settle_then_fail)

            task.add_done_callback(at_turn_end)

            holders: list[Connection] = []
            started = time.monotonic()
            try:
                if hold_pool:
                    holders.extend(app.state.session_engine.connect() for _ in range(main_capacity))
                else:
                    # A live turn: its fence is current and its task still runs.
                    live_before = _fence(observer, session_id)
                    record["live_reap"] = await asyncio.wait_for(worker.reap_once(), timeout=30)
                    record["live_fence_untouched"] = _fence(observer, session_id) == live_before
                    record["live_fence"] = live_before
                    record["live_job_status"] = app.state.composer_async_operation_authority.get(
                        session_id=session_id, operation_id=first["operation_id"]
                    ).status
                    model.release.set()
                await asyncio.sleep(_HOLD_SECONDS if hold_pool else 0.0)
            finally:
                for holder in holders:
                    holder.close()
            record["held_seconds"] = time.monotonic() - started
            record["turn_ended"] = await _wait_for(task.done, seconds=60)
            model.release.set()
            turn_leases = [lease for lease in adopted if lease.context == coordinator.authority.context]
            record["turn_leases"] = len(turn_leases)
            if turn_leases:
                lease = turn_leases[0]
                record["turn_lease"] = {
                    "closed": lease.closed,
                    "disposition": lease.disposition,
                    "renewal_error": _leaves(lease.renewal_error),
                }
            if probe == "commit_then_error":
                # Only the worker's own loop reaps here, so it is the caller
                # that meets the error.
                loop_task = worker._loop_task
                record["loop_ended"] = await _wait_for(loop_task.done, seconds=30)
                loop_failure = loop_task.exception() if record["loop_ended"] and not loop_task.cancelled() else None
                record["loop_failure"] = (
                    None
                    if loop_failure is None
                    else (type(loop_failure).__name__, None if loop_failure.__cause__ is None else type(loop_failure.__cause__).__name__)
                )
                watchdog = app.state.process_recovery.watchdog
                await _wait_for(lambda: bool(watchdog.reasons), seconds=10)
                record["recovery_reasons"] = list(watchdog.reasons)
            else:
                # Drive the recovery sweep directly; the worker's own loop is also
                # woken when the turn ends and may settle the job first.
                try:
                    record["reap_once"] = await asyncio.wait_for(worker.reap_once(), timeout=30)
                except Exception as reap_error:
                    record["reap_once"] = _leaves(reap_error)
            if probe == "peer_settles":
                peer.pop("stale")
                record["peer"]["fence_after_reap"] = _fence(observer, session_id)
                record["peer"]["coordinator_retained_after_reap"] = key in worker._required_coordinators
                # The peer's lease closes: it releases its own fence.
                session_service.session_operation_authority.release(peer["context"])
            settled = await _poll_terminal(app, session_id, first["operation_id"], seconds=15)
            record["first_job"] = _job_fields(settled)
            fence_after = _fence(observer, session_id)
            record["fence_after_settle"] = {
                "epoch_advanced": fence_after["operation_epoch"] > turn_fence["operation_epoch"],
                "same_identity_as_turn": all(
                    fence_after[name] == turn_fence[name] for name in ("operation_id", "lease_token", "operation_epoch")
                ),
                "released": fence_after["released"],
                "expired": fence_after["expired"],
                "operation_kind": fence_after["operation_kind"],
            }
            record["settled_epoch_is_fence_epoch"] = (
                settled is not None and settled.session_operation_epoch == fence_after["operation_epoch"]
            )
            record["member_live"] = _member_live(observer, instance_id)
            record["coordinator_retained_after_settle"] = key in worker._required_coordinators

            monkeypatch.setattr(session_service, "_session_operation_lease_seconds", _DEFAULT_LEASE_SECONDS)
            try:
                following = await SessionOperationLease.acquire(
                    session_service.session_operation_authority,
                    session_id=session_id,
                    operation_kind=SessionOperationKind.COMPOSE,
                    owner_instance_id=instance_id,
                    lease_seconds=_DEFAULT_LEASE_SECONDS,
                )
            except SessionOperationConflictError as conflict:
                record["next_acquire"] = type(conflict)
            else:
                record["next_acquire"] = "ok"
                await asyncio.wait_for(following.close(), timeout=30)
                record["next_acquire_disposition"] = following.disposition

            second = message_body("Explain this request without changing the pipeline.")
            accepted_second = await client.post(f"/api/sessions/{session_id}/messages", json=second)
            record["second_admission_status"] = accepted_second.status_code
            if accepted_second.status_code == 202:
                second_settled = await _poll_terminal(app, session_id, second["operation_id"], seconds=20)
                record["second_job"] = _job_fields(second_settled)
                # The row goes terminal before the owner task has finished its own close;
                # leaving the app before that would cancel the owner at shutdown.
                second_key = (session_id, second["operation_id"])
                record["second_owner_finished"] = await _wait_for(lambda: second_key not in worker._jobs, seconds=20)
            record["instance_draining_at_end"] = app.state.instance_draining.is_set()
            record["model_calls"] = model.calls
            record["model_cancelled"] = model.cancelled

    async def scenario() -> None:
        app = _build_app(databases, tmp_path)
        body_done = False
        try:
            async with app.router.lifespan_context(app):
                try:
                    await body(app)
                finally:
                    body_done = True
        except Exception as exit_error:
            if not body_done:
                raise
            record["lifespan_exit_error"] = _leaves(exit_error)

    asyncio.run(scenario())
    return record


def _lapsed_preconditions(record: dict[str, Any]) -> None:
    """The turn really lapsed: its own close could not release, and its projection stayed open."""
    at_end = record["at_turn_end"]
    assert at_end["fence_same_identity"] is True, record
    assert at_end["fence_expired"] is True, record
    assert at_end["fence_released"] is False, record
    assert at_end["coordinator_retained"] is True, record
    assert at_end["incomplete_sources"] == [RequiredWorkSource.TERMINAL_FAILURE_PROJECTION], record


def _released_and_next_turn_completes(record: dict[str, Any]) -> None:
    assert record["first_job"] is not None and record["first_job"]["status"] == "failed", record
    assert record["first_job"]["settled_by"] == "settle_own_lapsed", record
    # The worker's own loop reaps too: a second pass may acquire and close a
    # newer fence over the already-settled job, so only release is asserted.
    assert record["fence_after_settle"]["released"] is True, record
    assert record["coordinator_retained_after_settle"] is False, record
    assert record["next_acquire"] == "ok", record
    assert record["second_admission_status"] == 202, record
    assert record["second_job"] is not None and record["second_job"]["status"] == "completed", record
    assert record["second_owner_finished"] is True, record
    assert "lifespan_exit_error" not in record, record


@pytest.mark.timeout(240, method="thread")
def test_lapsed_turn_is_settled_and_released_by_the_reaper_and_the_next_turn_completes(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True)
    _lapsed_preconditions(record)
    _released_and_next_turn_completes(record)


@pytest.mark.timeout(240, method="thread")
def test_live_turn_is_untouched_by_the_reaper(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=False)
    assert record["live_reap"] == 0, record
    assert record["live_fence"]["released"] is False and record["live_fence"]["expired"] is False, record
    assert record["live_fence_untouched"] is True, record
    assert record["live_job_status"] == "running", record
    assert record["first_job"] is not None and record["first_job"]["status"] == "completed", record
    assert record["first_job"]["settled_by"] == "owner_terminal", record
    assert record["fence_after_settle"]["released"] is True, record
    assert record["second_job"] is not None and record["second_job"]["status"] == "completed", record
    assert record["second_owner_finished"] is True, record
    assert "lifespan_exit_error" not in record, record


@pytest.mark.timeout(240, method="thread")
def test_another_instance_never_settles_or_releases_this_instances_lapsed_turn(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True, probe="foreign_owner")
    _lapsed_preconditions(record)
    foreign = record["foreign"]
    assert foreign["raised"] is ComposerOperationFenceLost, record
    assert foreign["job_status"] == "running", record
    assert foreign["fence_after"]["released"] is False and foreign["fence_after"]["expired"] is True, record
    # This instance's own reaper still settles and releases it afterwards.
    _released_and_next_turn_completes(record)


@pytest.mark.timeout(240, method="thread")
def test_own_lapsed_settlement_without_the_reap_flag_keeps_the_fence(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True, probe="flagless_own")
    _lapsed_preconditions(record)
    flagless = record["flagless"]
    assert flagless["job"] is not None and flagless["job"]["status"] == "failed", record
    assert flagless["job"]["settled_by"] == "settle_own_lapsed", record
    assert flagless["fence_after"]["released"] is False, record
    assert record["fence_after_settle"]["released"] is False, record
    # Nothing discharged the owner's projection, so shutdown still fails closed.
    assert record["coordinator_retained_after_settle"] is True, record
    assert RequiredWorkIncomplete in record["lifespan_exit_error"], record


@pytest.mark.timeout(240, method="thread")
def test_a_peer_that_settles_first_releases_its_own_fence_and_the_reaper_leaves_custody_alone(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True, probe="peer_settles")
    _lapsed_preconditions(record)
    peer = record["peer"]
    assert peer["settled_by"] == "settle_lost", record
    after_peer = peer["fence_after_peer"]
    assert after_peer["operation_epoch"] > record["turn_fence"]["operation_epoch"], record
    assert after_peer["released"] is False and after_peer["expired"] is False, record
    # This instance's reaper met the peer's live fence, got the peer's terminal row back
    # from settle_own_lapsed, and changed nothing: no release of the peer's fence, no discharge.
    assert record["reap_once"] == 1, record
    assert peer["fence_after_reap"] == after_peer, record
    assert peer["coordinator_retained_after_reap"] is True, record
    assert record["first_job"] is not None and record["first_job"]["settled_by"] == "settle_lost", record
    # The settler released its own fence, and the session takes the next turn.
    assert record["fence_after_settle"]["released"] is True, record
    assert record["next_acquire"] == "ok", record
    assert record["second_job"] is not None and record["second_job"]["status"] == "completed", record
    assert record["second_owner_finished"] is True, record
    assert record["instance_draining_at_end"] is False, record
    # The owner's projection was not this reaper's to discharge: shutdown fails closed, as on base.
    assert RequiredWorkIncomplete in record["lifespan_exit_error"], record


@pytest.mark.timeout(240, method="thread")
def test_a_reap_settlement_that_commits_before_an_operational_error_ends_the_worker(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True, probe="commit_then_error")
    _lapsed_preconditions(record)
    assert record["committed_then_failed"] == ["settle_own_lapsed"], record
    assert record["loop_ended"] is True, record
    assert record["loop_failure"] == ("ComposerReapCustodyUnknown", "OperationalError"), record
    assert record["recovery_reasons"] == [RecoveryReason.REQUIRED_WORKER_LOST], record
    # The settlement committed on PostgreSQL: the job is failed and its fence released.
    assert record["first_job"] is not None and record["first_job"]["status"] == "failed", record
    assert record["first_job"]["settled_by"] == "settle_own_lapsed", record
    assert record["fence_after_settle"]["released"] is True, record
    # The worker ended before discharging the owner's projection.
    assert record["coordinator_retained_after_settle"] is True, record
