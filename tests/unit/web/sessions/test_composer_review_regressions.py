"""Real worker integration controls for the immutable Astra backend findings."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _HeldComposer, _worker


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_site", ("request_read", "clock_read", "request_decode"))
async def test_post_adopt_setup_fault_closes_actual_lease_without_provider(tmp_path, monkeypatch, fault_site: str) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    leases: list[SessionOperationLease] = []
    faulted = False
    adopt = SessionOperationLease.adopt

    async def capture_adopt(cls, *args, **kwargs):
        lease = await adopt(*args, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(SessionOperationLease, "adopt", classmethod(capture_adopt))
    original = authority.get_for_turn if fault_site == "request_read" else authority.get_with_database_now

    def failing_read(*args, **kwargs):
        nonlocal faulted
        if leases and not faulted:
            faulted = True
            raise OperationalError("SELECT", {}, RuntimeError("fault after adoption"))
        return original(*args, **kwargs)

    if fault_site != "request_decode":
        monkeypatch.setattr(authority, "get_for_turn" if fault_site == "request_read" else "get_with_database_now", failing_read)
    else:
        decode = SendMessageRequest.model_validate_json

        def failing_decode(*args, **kwargs):
            nonlocal faulted
            if leases and not faulted:
                faulted = True
                raise ValueError("admitted request decoding fault")
            return decode(*args, **kwargs)

        monkeypatch.setattr(SendMessageRequest, "model_validate_json", failing_decode)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        worker = _worker(app, authority)
        await worker.run_until_idle()
        assert faulted and len(leases) == 1
        assert leases[0].closed and leases[0]._renewal_task.done()
        assert not worker._jobs and composer.calls == 0
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "failed"
    finally:
        for lease in leases:
            if not lease.closed:
                await lease.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_site", ("title", "self_cancelled_title", "required_audit", "token_usage"))
async def test_owned_child_failure_outranks_persisted_stop_in_actual_failure_transaction(tmp_path, child_site: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
    from elspeth.web.sessions.composer_app_services import composer_app_services
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
    from elspeth.web.sessions.composer_operations import ComposerOperationError
    from tests.unit.web.sessions.test_routes import _make_app
    from tests.unit.web.sessions.test_run_composer_turn import _running_job

    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Priority receipt", "local")

    async def fail_child():
        if child_site == "self_cancelled_title":
            raise asyncio.CancelledError()
        raise RuntimeError("private required child failed")

    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        job.authority.request_cancel(session_id=session.id, operation_id=job.turn.operation_id, cancelled_failure=request_cancelled_error)
        with pytest.raises((AuditIntegrityError, ComposerOwnedSettlementFailure)) as captured:
            if child_site == "required_audit":
                await composer_turn._required_audit(job.observation, fail_child())
            elif child_site == "token_usage":
                from datetime import UTC, datetime
                from unittest.mock import patch

                from litellm import ModelResponse

                from elspeth.web.sessions import _auto_title

                async def fail_token_usage(**kwargs):
                    assert kwargs["session_operation_context"] is job.lease.context
                    assert kwargs["attempt_id"] == "owned-title-attempt"
                    raise RuntimeError("private token usage accounting failure")

                with patch.object(service, "settle_provider_attempt", new=fail_token_usage):
                    await _auto_title._charge_auto_title_response(
                        service,
                        job.lease.context,
                        model="test-title-model",
                        response=ModelResponse(choices=[{"message": {"role": "assistant", "content": "Title"}}]),
                        attempt_id="owned-title-attempt",
                        recorded_at=datetime.now(UTC),
                    )
            else:
                await composer_turn._join_auto_title(
                    asyncio.create_task(fail_child()),
                    session_id=session.id,
                    operation_id=job.turn.operation_id,
                    cancellation_observations=[],
                )
        worker = _worker(app, job.authority)
        terminal = await worker._settle_failure(composer_app_services(app), job.running, captured.value)
        assert terminal.status == "failed"
        assert ComposerOperationError.model_validate_json(terminal.result_json).http_status == 500
        assert terminal.failure_code != "request_cancelled"
        persisted = job.authority.get(session_id=session.id, operation_id=job.turn.operation_id)
        assert persisted is not None and persisted.result_sha256 == terminal.result_sha256
        assert persisted.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_completed_terminal_preserves_live_validation_from_exact_saved_state(tmp_path, monkeypatch) -> None:
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationEntry, ValidationSummary
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web.sessions.test_routes import _make_app, _make_composer_mock
    from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    composer.compose.return_value = ComposerResult(
        message="Validated response.",
        state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=2),
    )
    app.state.composer_service = composer
    validation = ValidationSummary(
        is_valid=True,
        errors=(),
        warnings=(ValidationEntry(component="source", message="Review this warning.", severity="medium"),),
        suggestions=(ValidationEntry(component="output", message="Review this suggestion.", severity="low"),),
    )

    async def validated_state(*args, **kwargs):
        return CompositionStateData(is_valid=True), validation

    monkeypatch.setattr(composer_turn, "_state_data_from_composer_state", validated_state)
    session = await service.create_session("alice", "Validation custody", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Save it.") as job:
        terminal = await _run(app, job)
        persisted = job.authority.get(session_id=session.id, operation_id=job.turn.operation_id)
        assert persisted is not None and persisted.result_json == terminal.result_json
        from elspeth.web.sessions.schemas import MessageWithStateResponse

        response = MessageWithStateResponse.model_validate_json(persisted.result_json)
        current = await service.get_current_state(session.id)
        assert response.state is not None and current is not None
        assert response.state.id == str(current.id)
        assert [entry.message for entry in response.state.validation_warnings] == ["Review this warning."]
        assert [entry.message for entry in response.state.validation_suggestions] == ["Review this suggestion."]
    composer.compose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fault", ("unknown", "audit_group", "reversed_audit_group", "known_fence", "body_weather_known_fence"))
async def test_actual_lease_release_fault_outranks_persisted_stop(tmp_path, monkeypatch, close_fault: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
    from elspeth.web.sessions.composer_operations import ComposerOperationError

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    original_release = service.session_operation_authority.release
    release_failed = False
    owners = []
    leases = []
    original_close_errors = []
    committed_before_release = []
    real_started_under_lease = worker._run_started_under_lease

    def failed_release(*args, **kwargs):
        nonlocal release_failed
        if not release_failed:
            release_failed = True
            active = authority.get(session_id=record.session_id, operation_id=record.operation_id)
            assert active is not None and active.status == "failed"
            committed_before_release.append(active)
            if close_fault in ("known_fence", "body_weather_known_fence"):
                original = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
                original_close_errors.append(original)
                raise original
            if close_fault in ("audit_group", "reversed_audit_group"):
                leaves = [RuntimeError("private body failure"), AuditIntegrityError("required close audit failed")]
                original = ExceptionGroup("owned body and close", leaves if close_fault == "audit_group" else list(reversed(leaves)))
                original_close_errors.append(original)
                raise original
            original = RuntimeError("private owned lease close failure")
            original_close_errors.append(original)
            raise original
        return original_release(*args, **kwargs)

    async def stop_before_close(services, running, lease, settlement, setup_ticket):
        assert setup_ticket is not None
        owner = asyncio.current_task()
        assert owner is not None
        owners.append(owner)
        leases.append(lease)
        authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        if close_fault == "body_weather_known_fence":
            setup_ticket.complete_owned()
            raise RuntimeError("ordinary provider weather")
        return await real_started_under_lease(services, running, lease, settlement, setup_ticket)

    monkeypatch.setattr(service.session_operation_authority, "release", failed_release)
    monkeypatch.setattr(worker, "_run_started_under_lease", stop_before_close)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        await worker.run_until_idle()
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert release_failed and not worker._jobs and composer.calls == 0
        assert terminal is not None and terminal.cancel_requested_at is not None
        # Actual source42 release follows a real committed Stop terminal; all late arms preserve it.
        assert terminal.failure_code == "request_cancelled"
        assert ComposerOperationError.model_validate_json(terminal.result_json).http_status == 499
        assert len(committed_before_release) == 1 and terminal == committed_before_release[0]
        assert len(owners) == 1 and owners[0].done()
        assert leases[0].closed and leases[0]._renewal_task.done()
        observed = owners[0].exception()
        assert observed is not None
        roots = []
        pending = [observed]
        while pending:
            current = pending.pop()
            if any(current is prior for prior in roots):
                continue
            roots.append(current)
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
        assert all(any(original is retained for retained in roots) for original in original_close_errors)
        assert leases[0].required_work is not None
        release_tickets = [ticket for ticket in leases[0].required_work.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(release_tickets) == 1 and release_tickets[0].complete
        assert any(error is original_close_errors[0] for error in release_tickets[0].errors)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_ingress_sql_retains_worker_slot_until_actual_thread_completion_on_lease_loss(tmp_path, monkeypatch) -> None:
    from elspeth.web.async_workers import outstanding_admissions
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    entered = threading.Event()
    release = threading.Event()
    leases = []
    adopt = SessionOperationLease.adopt

    async def capture_adopt(cls, *args, **kwargs):
        kwargs["renew_interval_seconds"] = 0.05
        lease = await adopt(*args, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(SessionOperationLease, "adopt", classmethod(capture_adopt))
    from sqlalchemy import event
    from sqlalchemy.sql.dml import Insert

    def park_actual_ingress(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and isinstance(compiled.statement, Insert) and compiled.statement.table.name == "message_ingress_receipts":
            entered.set()
            assert release.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", park_actual_ingress)
    operation_authority = service.session_operation_authority
    renew = operation_authority.renew

    def fail_renewal_after_ingress_started(*args, **kwargs):
        if entered.is_set():
            raise SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
        return renew(*args, **kwargs)

    monkeypatch.setattr(operation_authority, "renew", fail_renewal_after_ingress_started)
    task = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        worker = _worker(app, authority)
        baseline = outstanding_admissions()
        task = asyncio.create_task(worker.run_until_idle())
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set() and len(leases) == 1
        await asyncio.wait_for(leases[0].wait_until_lost(), timeout=2)
        await asyncio.sleep(0.35)
        assert not task.done() and len(worker._jobs) == 1
        assert outstanding_admissions() >= baseline + 1 and composer.calls == 0
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert not worker._jobs and outstanding_admissions() == baseline
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "failed"
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        event.remove(engine, "before_cursor_execute", park_actual_ingress)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ("stop", "shutdown"))
async def test_actual_worker_cancel_reason_preserves_progress_and_telemetry(tmp_path, monkeypatch, cause: str) -> None:
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    app, service, engine, _composer = _file_app(tmp_path)
    held = _HeldComposer()
    app.state.composer_service = held
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    terminals = []

    def record_terminal(status, *, endpoint):
        terminals.append((status, endpoint))

    monkeypatch.setattr(composer_turn, "_record_composer_request_terminal", record_terminal)
    worker = _worker(app, authority)
    task = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        task = asyncio.create_task(worker.run_until_idle())
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        if cause == "stop":
            authority.request_cancel(
                session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
            )
            worker.signal_local_cancel(session_id=record.session_id, operation_id=record.operation_id)
        else:
            await worker.stop()
        await asyncio.wait_for(task, timeout=5)
        latest = await app.state.composer_progress_registry.get_latest(str(record.session_id), "alice")
        assert latest.phase == ("cancelled" if cause == "stop" else "failed")
        assert latest.reason == ("client_cancelled" if cause == "stop" else "service_setup_failed")
        assert terminals == [("cancelled" if cause == "stop" else "failed", "send_message")]
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.failure_code == ("request_cancelled" if cause == "stop" else "worker_lost")
    finally:
        held.release.set()
        if task is not None and not task.done():
            await task
        engine.dispose()
