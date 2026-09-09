"""Diagnostic failures must not interrupt cancellation's lease cleanup."""

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import SessionOperationAuthority
from elspeth.web.sessions.routes import guided_operations


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_logger", [False, True], ids=["ordinary-diagnostics", "fatal-diagnostic"])
async def test_reconciliation_cancellation_closes_before_diagnostic_emission(monkeypatch, fatal_logger: bool) -> None:
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=str(uuid4()), operation_id=str(uuid4()), lease_token="test-lease", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    authority = MagicMock(spec=SessionOperationAuthority)
    authority.release.side_effect = OSError("close failed")
    lease = SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=10)
    started = asyncio.Event()
    finish = asyncio.Event()
    diagnostics: list[dict[str, str]] = []
    logger_error = AuditIntegrityError("diagnostic integrity failed")

    async def mutation() -> None:
        started.set()
        await finish.wait()
        raise RuntimeError("mutation failed")

    def record(_event: str, **fields: str) -> None:
        assert lease.closed
        authority.release.assert_called_once_with(context)
        diagnostics.append(fields)
        if fatal_logger:
            raise logger_error

    monkeypatch.setattr(guided_operations.slog, "error", record)
    request = asyncio.create_task(guided_operations.run_guided_reconciliation_mutation(lease, mutation()))
    await started.wait()
    request.cancel()
    await asyncio.sleep(0)
    finish.set()
    if fatal_logger:
        with pytest.raises(AuditIntegrityError) as caught:
            await request
        assert caught.value is logger_error
        cancellation = caught.value.__context__
        assert isinstance(cancellation, asyncio.CancelledError)
    else:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await request
        cancellation = cancelled.value
        assert [(entry["site"], entry["exc_class"]) for entry in diagnostics] == [
            ("reconciliation_mutation", "RuntimeError"),
            ("reconciliation_cancelled_close", "OSError"),
        ]
    assert len(cancellation.__notes__) == 2
    assert lease.closed
    authority.release.assert_called_once_with(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fatal_phase", ["mutation", "close", "both"])
async def test_reconciliation_integrity_failures_escape_after_close(monkeypatch, cancel: bool, fatal_phase: str) -> None:
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=str(uuid4()), operation_id=str(uuid4()), lease_token="test-lease", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    authority = MagicMock(spec=SessionOperationAuthority)
    mutation_error = AuditIntegrityError("mutation integrity") if fatal_phase != "close" else RuntimeError("ordinary mutation")
    close_error = AuditIntegrityError("close integrity") if fatal_phase != "mutation" else OSError("ordinary close")
    authority.release.side_effect = close_error
    lease = SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=10)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def mutation() -> None:
        started.set()
        await finish.wait()
        raise mutation_error

    def forbidden_logging(*_args, **_kwargs) -> None:
        raise AssertionError("a logger must not replace integrity failures")

    monkeypatch.setattr(guided_operations.slog, "error", forbidden_logging)
    request = asyncio.create_task(guided_operations.run_guided_reconciliation_mutation(lease, mutation()))
    await started.wait()
    if cancel:
        request.cancel()
        await asyncio.sleep(0)
    finish.set()
    if fatal_phase == "both":
        with pytest.raises(BaseExceptionGroup) as grouped:
            await request
        assert grouped.value.exceptions == (mutation_error, close_error)
    else:
        with pytest.raises(AuditIntegrityError) as caught:
            await request
        assert caught.value is (mutation_error if fatal_phase == "mutation" else close_error)
    assert lease.closed
    authority.release.assert_called_once_with(context)
