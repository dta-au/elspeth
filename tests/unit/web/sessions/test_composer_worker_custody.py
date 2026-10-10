"""Actual SQL admission custody, process death and detached worker priority."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.async_workers import outstanding_admissions, run_sync_in_worker
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    RequiredWorkTicket,
)
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_async_worker import _owned
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN
from tests.unit.web.composer.test_llm_sampling_config import _settings
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker
from tests.unit.web.sessions.test_run_composer_turn import _running_job
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service


@pytest.mark.asyncio
async def test_owned_sql_keeps_actual_admission_through_repeated_cancellation() -> None:
    entered = threading.Event()
    release = threading.Event()
    baseline = outstanding_admissions()

    def sql() -> str:
        entered.set()
        assert release.wait(timeout=5)
        return "actual SQL completed"

    task = asyncio.create_task(_owned(run_sync_in_worker(sql)))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert outstanding_admissions() == baseline + 1
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done()
        assert outstanding_admissions() == baseline + 1
        release.set()
        assert await asyncio.wait_for(task, timeout=2) == "actual SQL completed"
        assert outstanding_admissions() == baseline
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(AuditIntegrityError("required accounting failed"), id="accounting-integrity"),
        pytest.param(OperationalError("INSERT", {}, Exception("storage failed")), id="storage"),
        pytest.param(
            ExceptionGroup("body and close", [HTTPException(422, "ordinary"), AuditIntegrityError("title accounting")]),
            id="body-close-group",
        ),
    ),
)
async def test_authoritative_failure_outranks_explicit_stop(tmp_path, failure: BaseException) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        now = datetime.now(UTC)
        cancel_marked = replace(record, cancel_requested_at=now)
        projected = _worker(app, authority)._failure_for(cancel_marked, now, failure)
        assert projected.failure_code != "request_cancelled"
        assert projected.error_type in ("audit_integrity_error", "database_unavailable")
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_real_quota_settlement_fault_outranks_durable_stop(tmp_path) -> None:
    from litellm import ModelResponse

    original = RuntimeError("accounting child failed")
    quota_engine, quota_authority, quota_service = _quota_service(tmp_path)
    quota_session = quota_authority.create_session_with_initial_fence(
        user_id="alice", title="Quota priority", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = quota_authority.acquire(
        session_id=quota_session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=quota_service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    actual_finish = quota_service.finish_provider_attempt
    physical_attempts: list[str] = []

    async def provider(**_kwargs: object) -> ModelResponse:
        assert custody.attempt is not None
        physical_attempts.append(custody.attempt.attempt_id)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "An ordinary provider reply.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    async def failed_finish(
        _service,
        *,
        session_operation_context: SessionOperationContext,
        call: ComposerLLMCall,
        required_work: RequiredWorkTicket,
    ) -> None:
        assert session_operation_context is context
        assert required_work.key.source is RequiredWorkSource.PROVIDER_SETTLEMENT_SQL
        await actual_finish(session_operation_context=session_operation_context, call=call, required_work=required_work)
        assert required_work.complete
        raise original

    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    try:
        with (
            patch("litellm.acompletion", new=provider),
            patch.object(type(quota_service), "finish_provider_attempt", new=failed_finish),
            pytest.raises(ComposerOwnedSettlementFailure) as caught,
        ):
            await gateway._call_llm_with_audit([], [], timeout=5, recorder=None, provider_custody=custody)
        assert len(physical_attempts) == 1
        assert len(_ledger(quota_engine)) == 1
        coordinator.assert_completed()
    finally:
        quota_authority.release(context)
        quota_engine.dispose()
    assert caught.value.__cause__ is original
    app, service, engine, _composer = _file_app(tmp_path)
    try:
        session = await service.create_session("alice", "Quota priority", "local")
        async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
            job.authority.request_cancel(
                session_id=session.id, operation_id=job.turn.operation_id, cancelled_failure=request_cancelled_error
            )
            settled = await _worker(app, job.authority)._settle_failure(composer_app_services(app), job.running, caught.value)
            from elspeth.web.sessions.composer_operations import ComposerOperationError

            assert settled.status == "failed" and settled.failure_code != "request_cancelled"
            assert settled.cancel_requested_at is not None
            assert ComposerOperationError.model_validate_json(settled.result_json).http_status == 500
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_drain_retains_worker_slot_until_actual_terminal_sql_finishes(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    entered = threading.Event()
    release = threading.Event()
    baseline = outstanding_admissions()

    actual_terminal_sql = service._run_composer_terminal_sql

    async def retained_terminal_sql(_service, running, func, **kwargs):
        def held_sql():
            entered.set()
            assert release.wait(timeout=5)
            return func()

        return await actual_terminal_sql(running, held_sql, **kwargs)

    worker = _worker(app, authority)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        with patch.object(type(service), "_run_composer_terminal_sql", new=retained_terminal_sql):
            assert await worker._claim_once() == 1
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            assert composer.calls == 1
            owned_jobs = tuple(worker._jobs.values())
            assert len(owned_jobs) == 1
            await asyncio.wait_for(worker.stop(), timeout=0.5)
            assert not owned_jobs[0].done()
            assert len(worker._jobs) == 1
            assert outstanding_admissions() >= baseline + 1
            release.set()
            with pytest.raises(asyncio.CancelledError) as cancellation:
                await asyncio.wait_for(asyncio.gather(*owned_jobs), timeout=2)
            assert cancellation.value.args == (COMPOSER_SHUTDOWN,)
            await asyncio.sleep(0)
            assert not worker._jobs
            assert outstanding_admissions() == baseline
        final = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert final is not None and final.status == "completed"
        assert final.session_id == record.session_id and final.operation_id == record.operation_id
        assert final.cancel_requested_at is None and composer.calls == 1
    finally:
        release.set()
        if worker._jobs:
            await asyncio.gather(*worker._jobs.values(), return_exceptions=True)
        engine.dispose()


_CRASH_PROCESS = """
import sys
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
import structlog
engine = create_session_engine('sqlite:///' + sys.argv[1])
authority = ComposerAsyncOperationAuthority(engine,owner_instance_id='crashed-owner',claim_lease_seconds=5)
claims = authority.claim_next(limit=1)
assert len(claims) == 1
if sys.argv[2] == 'started':
    service = SessionServiceImpl(engine,telemetry=build_sessions_telemetry(),log=structlog.get_logger('crash'),owner_instance_id='crashed-owner',session_operation_lease_seconds=1)
    service.session_operation_authority.start_composer_async_operation(claims[0],owner_instance_id='crashed-owner',lease_seconds=1,auth_provider_type='local')
print(sys.argv[2],flush=True)
engine.dispose()
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ("claimed", "started"))
async def test_independent_process_crash_window_never_replays_running_work(tmp_path, window: str) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        database_path = engine.url.database
        assert database_path is not None
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", _CRASH_PROCESS, database_path, window],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == window
        before = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert before is not None and before.status == ("running" if window == "started" else "queued")
        assert before.claim_owner_instance_id == "crashed-owner"
        # Sample the actual database clock/liveness query until the dead claim
        # or SOL expires; no test clock can manufacture that proof.
        for _ in range(140):
            if window == "started":
                expired = authority.list_expired_running(limit=1)
                ready = bool(expired)
            else:
                _current, now = authority.get_with_database_now(session_id=record.session_id, operation_id=record.operation_id)
                ready = before.claim_expires_at is not None and now >= before.claim_expires_at
            if ready:
                break
            await asyncio.sleep(0.05)
        assert ready
        worker = _worker(app, authority)
        await worker.run_until_idle()
        final = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert final is not None
        if window == "started":
            assert final.status == "failed" and final.failure_code == "worker_lost"
            assert composer.calls == 0
            assert final.attempt == 1
        else:
            assert final.status == "completed"
            assert composer.calls == 1
            assert final.attempt == 2
    finally:
        engine.dispose()
