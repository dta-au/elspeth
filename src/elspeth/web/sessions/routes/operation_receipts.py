"""Retry-safe ordinary fork/revert request ownership and replay."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Never, overload
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.operation_receipts import operation_receipt_request_hash, operation_receipt_response_hash
from elspeth.web.sessions.protocol import (
    OperationReceiptActive,
    OperationReceiptClaimed,
    OperationReceiptCompleted,
    OperationReceiptConflictError,
    OperationReceiptFailed,
    OperationReceiptFailureCode,
    OperationReceiptFence,
    OperationReceiptFenceLostError,
    OperationReceiptKind,
    OperationReceiptOutcome,
    OperationReceiptResult,
    OperationReceiptSettlementConflictError,
    OperationReceiptTakenOver,
    SessionServiceProtocol,
)

_ACTOR = "composer_route"
_GUARD_ACTOR = "composer_route_guard"
_LEASE_SECONDS = 300
_POLL_SECONDS = 0.1
_SAFE_FAILURES: dict[OperationReceiptFailureCode, tuple[int, str]] = {
    "stale_conflict": (409, "The session changed before this operation could complete."),
    "integrity_error": (500, "The operation could not verify its audit evidence."),
    "custody_error": (500, "The operation could not establish result custody."),
    "quota_exceeded": (413, "The operation exceeded the session storage quota."),
    "operation_failed": (500, "The operation failed."),
    "request_cancelled": (499, "The request was cancelled before durable staging completed."),
}


@dataclass(frozen=True, slots=True)
class OperationReceiptLease:
    fence: OperationReceiptFence
    session_lease: SessionOperationLease

    @property
    def session_operation_context(self) -> SessionOperationContext:
        return self.session_lease.context

    async def close(self) -> None:
        await self.session_lease.close()


@dataclass(frozen=True, slots=True)
class OperationReceiptExpired:
    attempt: int


def operation_receipt_failure_error(outcome: OperationReceiptFailed) -> HTTPException:
    if outcome.failure_code not in _SAFE_FAILURES:
        raise AuditIntegrityError("Operation receipt returned an unknown failure code")
    status, detail = _SAFE_FAILURES[outcome.failure_code]
    return HTTPException(
        status_code=status,
        detail={"error_type": "session_operation_terminal_failure", "failure_code": outcome.failure_code, "detail": detail},
    )


def raise_operation_receipt_failure(outcome: OperationReceiptFailed) -> Never:
    raise operation_receipt_failure_error(outcome)


async def _join_shielded[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            if not task.done():
                raise
    return task.result()


async def _replay_verified[ResponseT: BaseModel](
    outcome: OperationReceiptCompleted,
    replay: Callable[[OperationReceiptResult], Awaitable[ResponseT]],
    after_verified: Callable[[OperationReceiptResult], Awaitable[None]] | None,
) -> ResponseT:
    response = await replay(outcome.result)
    if operation_receipt_response_hash(response) != outcome.response_hash:
        raise AuditIntegrityError("Operation receipt replay response hash does not match stored evidence")
    if after_verified is not None:
        await after_verified(outcome.result)
    return response


@dataclass(slots=True)
class _OperationReceiptLeaseGuard:
    service: SessionServiceProtocol
    lease: OperationReceiptLease

    async def finish_active_exception(self) -> None:
        primary = sys.exception()
        cleanup_error: BaseException | None = None
        terminalized = False
        if primary is None:
            failure_code: OperationReceiptFailureCode = "operation_failed"
        elif isinstance(primary, OperationReceiptSettlementConflictError) or (
            isinstance(primary, HTTPException) and primary.status_code == 409
        ):
            failure_code = "stale_conflict"
        elif isinstance(primary, asyncio.CancelledError):
            failure_code = "request_cancelled"
        elif isinstance(primary, AuditIntegrityError):
            failure_code = "integrity_error"
        else:
            failure_code = "operation_failed"
        if not isinstance(primary, OperationReceiptFenceLostError):
            try:
                await _join_shielded(
                    asyncio.create_task(
                        self.service.fail_operation_receipt(
                            self.lease.fence,
                            failure_code=failure_code,
                            actor=_GUARD_ACTOR,
                            session_operation_context=self.lease.session_operation_context,
                        ),
                        name="operation-receipt-guard-fail",
                    )
                )
            except OperationReceiptFenceLostError:
                terminalized = True
            except BaseException as exc:
                cleanup_error = exc
        try:
            await _join_shielded(asyncio.create_task(self.lease.close(), name="operation-receipt-guard-close"))
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error = BaseExceptionGroup("Operation receipt cleanup failures", [cleanup_error, exc])
        if cleanup_error is not None:
            if isinstance(cleanup_error, contract_errors.TIER_1_ERRORS) or primary is None:
                raise cleanup_error from primary
            primary.add_note(f"Operation receipt cleanup also failed with {type(cleanup_error).__name__}.")
        if primary is None and not terminalized:
            raise AuditIntegrityError("Route returned before its operation receipt became terminal")


def operation_receipt_lease_guard(*, service: SessionServiceProtocol, lease: OperationReceiptLease) -> _OperationReceiptLeaseGuard:
    return _OperationReceiptLeaseGuard(service=service, lease=lease)


@overload
async def reserve_or_replay_operation_receipt[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: OperationReceiptKind,
    request: BaseModel,
    replay: Callable[[OperationReceiptResult], Awaitable[ResponseT]],
    after_verified: Callable[[OperationReceiptResult], Awaitable[None]] | None = None,
    reserve_if_absent: Literal[False],
    takeover_expired: Literal[False],
) -> OperationReceiptLease | OperationReceiptExpired | ResponseT | None: ...


@overload
async def reserve_or_replay_operation_receipt[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: OperationReceiptKind,
    request: BaseModel,
    replay: Callable[[OperationReceiptResult], Awaitable[ResponseT]],
    after_verified: Callable[[OperationReceiptResult], Awaitable[None]] | None = None,
    reserve_if_absent: bool = True,
    takeover_expired: Literal[True] = True,
) -> OperationReceiptLease | ResponseT | None: ...


async def reserve_or_replay_operation_receipt[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: OperationReceiptKind,
    request: BaseModel,
    replay: Callable[[OperationReceiptResult], Awaitable[ResponseT]],
    after_verified: Callable[[OperationReceiptResult], Awaitable[None]] | None = None,
    reserve_if_absent: bool = True,
    takeover_expired: bool = True,
) -> OperationReceiptLease | OperationReceiptExpired | ResponseT | None:
    """Claim, synchronously join, or replay one strict fork/revert response."""
    if not takeover_expired and reserve_if_absent:
        raise ValueError("Non-taking-over receipt lookup must not reserve an absent request")
    request_fields = request.model_dump(mode="python")
    operation_id = request_fields.get("operation_id")
    if type(operation_id) is not str:
        raise AuditIntegrityError("Operation receipt request requires a string operation_id")
    request_hash = operation_receipt_request_hash(session_id=session_id, kind=kind, request=request)

    async def reserve(lease: SessionOperationLease) -> OperationReceiptOutcome:
        try:
            return await service.reserve_operation_receipt(
                session_id=session_id,
                operation_id=operation_id,
                kind=kind,
                request_hash=request_hash,
                actor=_ACTOR,
                lease_seconds=_LEASE_SECONDS,
                session_operation_context=lease.context,
            )
        except OperationReceiptConflictError as exc:
            raise HTTPException(status_code=409, detail="Operation id is already bound to a different request.") from exc

    try:
        outcome: OperationReceiptOutcome | None = await service.get_operation_receipt(
            session_id=session_id,
            operation_id=operation_id,
            kind=kind,
            request_hash=request_hash,
        )
    except OperationReceiptConflictError as exc:
        raise HTTPException(status_code=409, detail="Operation id is already bound to a different request.") from exc
    observed_by_get = True
    session_lease: SessionOperationLease | None = None

    async def acquire_and_reserve() -> OperationReceiptOutcome:
        nonlocal session_lease
        operation_kind = SessionOperationKind.SESSION_FORK if kind == "session_fork" else SessionOperationKind.COMPOSE
        session_lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session_id,
            operation_kind=operation_kind,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        task = asyncio.create_task(reserve(session_lease), name="operation-receipt-reserve")
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            held = session_lease
            session_lease = None
            try:
                claim = await _join_shielded(task)
                if isinstance(claim, (OperationReceiptClaimed, OperationReceiptTakenOver)):
                    await _join_shielded(
                        asyncio.create_task(
                            service.fail_operation_receipt(
                                claim.fence,
                                failure_code="request_cancelled",
                                actor=_ACTOR,
                                session_operation_context=held.context,
                            ),
                            name="operation-receipt-cancelled-reserve-fail",
                        )
                    )
            finally:
                await _join_shielded(asyncio.create_task(held.close(), name="operation-receipt-cancelled-reserve-close"))
            raise cancellation from None
        except BaseException as primary:
            held = session_lease
            session_lease = None
            try:
                await _join_shielded(asyncio.create_task(held.close(), name="operation-receipt-failed-reserve-close"))
            except BaseException as close_error:
                raise BaseExceptionGroup("Operation receipt reserve and close both failed", [primary, close_error]) from None
            raise

    if outcome is None:
        if not reserve_if_absent:
            return None
        outcome = await acquire_and_reserve()
        observed_by_get = False
    while True:
        if isinstance(outcome, (OperationReceiptClaimed, OperationReceiptTakenOver)):
            if session_lease is None:
                raise AuditIntegrityError("Operation receipt claim has no owning session lease")
            return OperationReceiptLease(fence=outcome.fence, session_lease=session_lease)
        if type(outcome) is OperationReceiptCompleted:
            if session_lease is not None:
                await session_lease.close()
            return await _replay_verified(outcome, replay, after_verified)
        if type(outcome) is OperationReceiptFailed:
            if session_lease is not None:
                await session_lease.close()
            raise_operation_receipt_failure(outcome)
        if type(outcome) is not OperationReceiptActive:
            raise AuditIntegrityError("Operation receipt returned an unknown outcome")
        if session_lease is not None:
            await session_lease.close()
            session_lease = None
        if outcome.expired and observed_by_get:
            if not takeover_expired:
                return OperationReceiptExpired(attempt=outcome.attempt)
            outcome = await acquire_and_reserve()
            observed_by_get = False
            continue
        await asyncio.sleep(_POLL_SECONDS)
        try:
            observed = await service.get_operation_receipt(
                session_id=session_id,
                operation_id=operation_id,
                kind=kind,
                request_hash=request_hash,
            )
        except OperationReceiptConflictError as exc:
            raise HTTPException(status_code=409, detail="Operation id is already bound to a different request.") from exc
        if observed is None:
            raise AuditIntegrityError("Operation receipt disappeared while a caller was joining it")
        outcome = observed
        observed_by_get = True
