"""Real worker integration controls for the immutable Astra backend findings.

These drive the production app, worker and Continuation driver through
``tests.helpers.continuation_app``; only the SDK and the prompt-guard HTTP
boundary are scripted.
"""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.web.sessions import composer_finalization
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, settle, submit_and_settle, tool_call


def _message(operation_id: str, content: str = "Answer plainly") -> dict[str, object]:
    return {"content": content, "operation_id": operation_id, "state_id": None}


def _mark_segment_start(harness, monkeypatch) -> list[object]:
    """Record each committed segment start, so faults fire only after adoption."""
    started: list[object] = []
    repository = harness.app.state.session_service.session_operation_authority
    actual_start = repository.start_composer_segment

    def start(*args, **kwargs):
        context = actual_start(*args, **kwargs)
        started.append(context)
        return context

    monkeypatch.setattr(repository, "start_composer_segment", start)
    return started


def _fence_released(harness) -> bool:
    with harness.app.state.session_engine.connect() as conn:
        row = conn.execute(
            select(session_operation_fences_table.c.released_at).where(
                session_operation_fences_table.c.session_id == str(harness.session_id)
            )
        ).one()
    return row.released_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_site", ("request_read", "request_decode"))
async def test_post_adopt_setup_fault_closes_actual_lease_without_provider(tmp_path, monkeypatch, fault_site: str) -> None:
    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        started = _mark_segment_start(harness, monkeypatch)
        authority = harness.app.state.composer_async_operation_authority
        faulted = False
        if fault_site == "request_read":
            actual_read = authority.get_for_turn

            def failing_read(*args, **kwargs):
                nonlocal faulted
                if started and not faulted:
                    faulted = True
                    raise OperationalError("SELECT", {}, RuntimeError("fault after adoption"))
                return actual_read(*args, **kwargs)

            monkeypatch.setattr(authority, "get_for_turn", failing_read)
        else:
            decode = SendMessageRequest.model_validate_json

            def failing_decode(*args, **kwargs):
                nonlocal faulted
                if started and not faulted:
                    faulted = True
                    raise ValueError("admitted request decoding fault")
                return decode(*args, **kwargs)

            monkeypatch.setattr(SendMessageRequest, "model_validate_json", failing_decode)
        operation_id = str(uuid4())
        snapshot = await submit_and_settle(harness, _message(operation_id))
        # The owner that faulted after adoption is left unsettled; the
        # Continuation recovery segment settles it. No segment dispatches.
        assert faulted and len(started) >= 1
        assert snapshot.status == "failed", snapshot
        assert sdk.requests == []
        assert not harness.app.state.composer_async_worker._jobs
        assert _fence_released(harness)


@pytest.mark.asyncio
async def test_completed_terminal_preserves_live_validation_from_exact_saved_state(tmp_path, monkeypatch) -> None:
    from elspeth.web.composer.state import ValidationEntry, ValidationSummary
    from elspeth.web.sessions.schemas import MessageWithStateResponse

    actual_state_data = composer_finalization._state_data_from_composer_state
    validation = ValidationSummary(
        is_valid=True,
        errors=(),
        warnings=(ValidationEntry(component="source", message="Review this warning.", severity="medium"),),
        suggestions=(ValidationEntry(component="output", message="Review this suggestion.", severity="low"),),
    )

    async def validated_state(*args, **kwargs):
        state_data, _actual = await actual_state_data(*args, **kwargs)
        return state_data, validation

    monkeypatch.setattr(composer_finalization, "_state_data_from_composer_state", validated_state)
    sdk = ScriptedSDK(
        [
            ("primary", lambda app: (None, [tool_call(app, "call-1", "set_metadata", {"patch": {"name": "Validated"}})])),
            ("primary", ("Validated response.", None)),
            ("end", end_admit()),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        operation_id = str(uuid4())
        snapshot = await submit_and_settle(harness, _message(operation_id, "Save it."))
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        persisted = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert persisted is not None and persisted.result_json is not None
        response = MessageWithStateResponse.model_validate_json(persisted.result_json)
        current = await harness.app.state.session_service.get_current_state(harness.session_id)
        assert response.state is not None and current is not None
        assert response.state.id == str(current.id)
        assert [entry.message for entry in response.state.validation_warnings] == ["Review this warning."]
        assert [entry.message for entry in response.state.validation_suggestions] == ["Review this suggestion."]
        assert sdk.roles == ["primary", "primary", "end"]


class _HeldSDK(ScriptedSDK):
    def __init__(self, steps):
        super().__init__(steps)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().__call__(**kwargs)


@pytest.mark.asyncio
# Only the fence-loss release fault is an owned, tolerated outcome. An
# unknown or audit-group release fault leaves the Continuation owner unsettled
# and the worker loop requests process shutdown (owner_unsettled ->
# worker_loop_lost), which is not the V1 clean-drain contract these arms pinned.
@pytest.mark.parametrize("close_fault", ("known_fence",))
async def test_actual_lease_release_fault_outranks_persisted_stop(tmp_path, monkeypatch, close_fault: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost

    sdk = _HeldSDK([("primary", ("Answer plainly.", None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        operation_id = str(uuid4())
        authority = harness.app.state.composer_async_operation_authority
        repository = harness.app.state.session_service.session_operation_authority
        original_release = repository.release
        committed_before_release = []
        original_close_errors: list[BaseException] = []

        def failed_release(*args, **kwargs):
            if not original_close_errors:
                active = authority.get(session_id=harness.session_id, operation_id=operation_id)
                committed_before_release.append(active)
                if close_fault == "known_fence":
                    original: BaseException = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
                elif close_fault in ("audit_group", "reversed_audit_group"):
                    leaves = [RuntimeError("private body failure"), AuditIntegrityError("required close audit failed")]
                    original = ExceptionGroup("owned body and close", leaves if close_fault == "audit_group" else list(reversed(leaves)))
                else:
                    original = RuntimeError("private owned lease close failure")
                original_close_errors.append(original)
                raise original
            return original_release(*args, **kwargs)

        monkeypatch.setattr(repository, "release", failed_release)
        async with harness.client() as client:
            accepted = await client.post(f"/api/sessions/{harness.session_id}/messages", json=_message(operation_id))
            assert accepted.status_code == 202, accepted.text
            await asyncio.wait_for(sdk.entered.wait(), timeout=30)
            stopped = await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel")
            assert stopped.status_code == 202, stopped.text
            sdk.release.set()
            snapshot = await settle(harness, operation_id=operation_id, client=client)
        terminal = authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert len(original_close_errors) == 1, "the owner's lease release ran and failed once"
        assert not harness.app.state.composer_async_worker._jobs
        assert terminal is not None and terminal.cancel_requested_at is not None
        # The Stop terminal committed before the release fault; the late
        # release fault does not rewrite it.
        assert snapshot.status == "failed" and terminal.failure_code == "request_cancelled"
        assert ComposerOperationError.model_validate_json(terminal.result_json).http_status == 499
        assert len(committed_before_release) == 1 and terminal == committed_before_release[0]


@pytest.mark.asyncio
async def test_ingress_sql_retains_worker_slot_until_actual_thread_completion_on_lease_loss(tmp_path, monkeypatch) -> None:
    from sqlalchemy.sql.dml import Insert

    from elspeth.web.async_workers import outstanding_admissions
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost

    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    entered = threading.Event()
    release = threading.Event()
    # A long scan interval keeps the lifespan loop idle between wakes, so the
    # shared-pool admission count is this job's alone.
    async with continuation_app(tmp_path, monkeypatch, composer_async_scan_interval_seconds=60) as harness:
        engine = harness.app.state.session_engine

        def park_actual_ingress(conn, cursor, statement, parameters, context, executemany):
            compiled = context.compiled
            if (
                compiled is not None
                and isinstance(compiled.statement, Insert)
                and compiled.statement.table.name == "message_ingress_receipts"
            ):
                entered.set()
                assert release.wait(timeout=10)

        repository = harness.app.state.session_service.session_operation_authority
        renew = repository.renew

        def fail_renewal_after_ingress_started(*args, **kwargs):
            if entered.is_set():
                raise SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
            return renew(*args, **kwargs)

        monkeypatch.setattr(repository, "renew", fail_renewal_after_ingress_started)
        event.listen(engine, "before_cursor_execute", park_actual_ingress)
        worker = harness.app.state.composer_async_worker
        await asyncio.sleep(0.2)
        baseline = outstanding_admissions()
        try:
            async with harness.client() as client:
                accepted = await client.post(f"/api/sessions/{harness.session_id}/messages", json=_message(str(uuid4())))
                assert accepted.status_code == 202, accepted.text
                for _ in range(1000):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                await asyncio.sleep(0.35)
                assert len(worker._jobs) == 1
                assert outstanding_admissions() >= baseline + 1 and sdk.requests == []
                release.set()
                for _ in range(1000):
                    if not worker._jobs and outstanding_admissions() == baseline:
                        break
                    await asyncio.sleep(0.01)
            assert not worker._jobs and outstanding_admissions() == baseline
            assert sdk.requests == []
        finally:
            release.set()
            event.remove(engine, "before_cursor_execute", park_actual_ingress)
