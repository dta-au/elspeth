"""HTTP lifecycle adapter for retry-safe composer mutations.

This module deliberately sits outside ``composer/guided.py``.  Several legacy
guided handlers carry governance fingerprints whose AST locations are stable;
centralising the retry protocol here avoids inserting module-level definitions
above those handlers while the pre-release cutover replaces them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Never, overload
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.guided_operations import guided_operation_request_hash
from elspeth.web.sessions.protocol import (
    GuidedOperationActive,
    GuidedOperationClaimed,
    GuidedOperationCompleted,
    GuidedOperationConflictError,
    GuidedOperationFailed,
    GuidedOperationFailureCode,
    GuidedOperationFence,
    GuidedOperationFenceLostError,
    GuidedOperationKind,
    GuidedOperationOutcome,
    GuidedOperationResult,
    GuidedOperationSettlementConflictError,
    GuidedOperationTakenOver,
    SessionServiceProtocol,
)

_ACTOR = "composer_route"
_LEASE_SECONDS = 300
_POLL_SECONDS = 0.05

# Ceiling for a NEW guided operation waiting on the per-session admission
# lock. The admission lock is held across the whole respond settlement —
# including the in-request pipeline planner, observed at 200s+ live — so an
# unbounded wait turns a competing different-body request (double-click race,
# second tab) into a silent multi-minute hang that then dies stale. Same-body
# retries never wait here: they join or replay via the pre-admission lookup.
# The ceiling absorbs ordinary quick settlements; a planner-length hold
# answers fast with the coded conflict below.
GUIDED_RESPOND_ADMISSION_WAIT_SECONDS: float = 10.0


@contextlib.asynccontextmanager
async def bounded_admission_guard(lock: asyncio.Lock) -> AsyncIterator[None]:
    """Acquire a per-session admission lock with a hard ceiling.

    On timeout, answer the closed conflict envelope instead of queueing
    behind a multi-minute settlement. In-process lock, single-instance
    deployment (one container per org); the DB-level operation reserve
    remains the cross-process correctness fence — this guard is the
    fast-answer UX boundary, not the integrity boundary.
    """
    try:
        await asyncio.wait_for(lock.acquire(), timeout=GUIDED_RESPOND_ADMISSION_WAIT_SECONDS)
    except TimeoutError:
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": "guided_operation_conflict",
                "code": "operation_in_progress",
                "detail": (
                    "Another guided operation is still settling for this session. "
                    "Wait for it to finish, then reload the session state and retry with a new operation id."
                ),
            },
        ) from None
    try:
        yield
    finally:
        lock.release()


_SAFE_FAILURES: dict[str, tuple[int, str]] = {
    "provider_unavailable": (503, "The provider is unavailable. Retry with a new operation id."),
    "provider_timeout": (504, "The operation timed out. Retry with a new operation id."),
    # "Retry the request." rather than "Retry with a new operation id.": the
    # client already mints a fresh operation id on every re-click, so naming the
    # id taught the reader an internal protocol detail they cannot act on.
    "invalid_provider_response": (502, "The provider returned an invalid response. Retry the request."),
    # PERMANENT by construction — a deployment policy refused this pipeline, so
    # the copy must not offer a retry and must not blame the provider. Kept in
    # lockstep with the freeform mirror
    # (``routes/_helpers.py::_FREEFORM_PLANNER_FAILURE_HTTP``) up to one word:
    # "highlighted" is guided-only, because only the guided review UI pins the
    # blocked component; freeform has no component highlight.
    "policy_blocked": (
        422,
        "This pipeline is blocked by a deployment policy and cannot be built as configured. "
        "Change the highlighted component — retrying will fail the same way.",
    ),
    "stale_conflict": (409, "The guided state changed before settlement. Reload the authoritative state."),
    "integrity_error": (500, "The operation failed an integrity check."),
    "custody_error": (500, "The operation could not establish result custody."),
    "quota_exceeded": (413, "The operation exceeded the session storage quota."),
    "operation_failed": (500, "The operation failed."),
    "request_cancelled": (499, "The request was cancelled before durable staging completed."),
}


@dataclass(frozen=True, slots=True)
class GuidedOperationLease:
    """A route owns both renewable authorities for one guided operation."""

    fence: GuidedOperationFence
    session_lease: SessionOperationLease

    @property
    def session_operation_context(self) -> SessionOperationContext:
        return self.session_lease.context

    async def close(self) -> None:
        """Release session authority after the guided row is terminal."""
        await self.session_lease.close()


@dataclass(frozen=True, slots=True)
class GuidedOperationExpired:
    """Replay-only lookup found an existing operation whose lease expired."""

    attempt: int


def guided_response_hash(response: BaseModel) -> str:
    """Hash the complete strict HTTP response domain used for replay."""

    config = type(response).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid":
        raise AuditIntegrityError("Guided operation replay requires a strict, extra-forbid response DTO")
    # Re-validate the emitted representation strictly.  Constructed Pydantic
    # instances can bypass validation through ``model_construct``; a replay
    # hash must never bless such an object as Tier 1 response evidence.
    strict_response = type(response).model_validate(response.model_dump(mode="python"), strict=True)
    return stable_hash(strict_response.model_dump(mode="json"))


def raise_guided_operation_failure(
    outcome: GuidedOperationFailed,
    *,
    unproducible_output_fields: tuple[str, ...] = (),
) -> Never:
    """Raise the closed HTTP failure represented by a terminal operation.

    ``unproducible_output_fields`` names the reviewed output fields no reviewed
    source declares or observes, when the planner exhausted its budget on a
    request that carried that gap (R2-F4). Without it the operator reads only
    "the provider returned an invalid response" — a dead end — while the server
    holds the exact, actionable cause. Guided-only by construction: freeform has
    no reviewed output, so the mirrored freeform table in
    ``routes/_helpers.py::_FREEFORM_PLANNER_FAILURE_HTTP`` deliberately has no
    counterpart. That is the same guided-only divergence as "highlighted" in the
    ``policy_blocked`` copy, and for the same reason — this surface knows a fact
    the other cannot.

    The names are the operator's own step-2 ``custom_inputs`` strings (field
    review admits ``chosen`` only from the reviewed sources' observed columns
    and forbids custom names from overlapping them), so returning them to the
    same operator discloses nothing. They ride BOTH as a structured field for
    API consumers and appended to ``detail``, because the web client projects
    only a fixed set of envelope keys and would otherwise drop the structured
    form silently.
    """

    safe = _SAFE_FAILURES.get(outcome.failure_code)
    if safe is None:
        raise AuditIntegrityError("Guided operation returned an unknown failure code")
    if type(unproducible_output_fields) is not tuple or any(type(field) is not str for field in unproducible_output_fields):
        raise TypeError("unproducible_output_fields must be an exact string tuple")
    status_code, detail = safe
    body: dict[str, object] = {
        "error_type": "guided_operation_terminal_failure",
        "failure_code": outcome.failure_code,
        "detail": detail,
    }
    if unproducible_output_fields:
        body["unproducible_output_fields"] = list(unproducible_output_fields)
        # States only what is known — that nothing reviewed supplies these
        # fields — never that the pipeline "would fail at runtime", which this
        # surface cannot prove for a source whose field inventory is unknown
        # rather than empty.
        body["detail"] = (
            f"{detail} No reviewed source declares or observes these output fields: "
            f"{', '.join(unproducible_output_fields)}. Add a step that produces them, or remove them "
            "from the output's fields."
        )
    raise HTTPException(status_code=status_code, detail=body)


async def _replay_completed[ResponseT: BaseModel](
    outcome: GuidedOperationCompleted,
    replay: Callable[[GuidedOperationResult], Awaitable[ResponseT]],
) -> ResponseT:
    response = await replay(outcome.result)
    if guided_response_hash(response) != outcome.response_hash:
        raise AuditIntegrityError("Guided operation replay response hash does not match its stored response hash")
    return response


async def _join_shielded_task_after_cancellation[T](task: asyncio.Task[T]) -> T:
    """Join owned cleanup despite repeated cancellation of the caller."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_guided_reconciliation_mutation[T](
    session_lease: SessionOperationLease,
    mutation: Awaitable[T],
) -> T:
    """Finish one atomic reconciliation before releasing its session fence."""

    async def execute_mutation() -> T:
        return await mutation

    mutation_task: asyncio.Task[T] = asyncio.create_task(
        execute_mutation(),
        name="guided-reconciliation-mutation",
    )
    try:
        result = await asyncio.shield(mutation_task)
    except asyncio.CancelledError as cancellation:
        try:
            await _join_shielded_task_after_cancellation(mutation_task)
        except BaseException as cleanup_error:
            cancellation.add_note(f"Guided reconciliation cancellation cleanup also failed with {type(cleanup_error).__name__}.")
        close_task = asyncio.create_task(session_lease.close(), name="guided-reconciliation-session-close")
        try:
            await _join_shielded_task_after_cancellation(close_task)
        except BaseException as close_error:
            cancellation.add_note(f"Guided reconciliation session cleanup also failed with {type(close_error).__name__}.")
        raise cancellation from None
    except BaseException as primary:
        try:
            await session_lease.close()
        except BaseException as close_error:
            if close_error is not primary:
                primary.add_note(f"Guided reconciliation session cleanup also failed with {type(close_error).__name__}.")
        raise
    await session_lease.close()
    return result


def _guided_failure_code_for_exception(error: BaseException) -> GuidedOperationFailureCode:
    if isinstance(error, asyncio.CancelledError):
        return "request_cancelled"
    if isinstance(error, GuidedOperationSettlementConflictError) or (isinstance(error, HTTPException) and error.status_code == 409):
        return "stale_conflict"
    if isinstance(error, (AuditIntegrityError, InvariantError)):
        return "integrity_error"
    return "operation_failed"


@dataclass(slots=True)
class _GuidedOperationLeaseGuard:
    service: SessionServiceProtocol
    lease: GuidedOperationLease

    async def __aenter__(self) -> _GuidedOperationLeaseGuard:
        return self

    async def finish(self, primary: BaseException | None) -> None:
        """Run exit cleanup from an existing route try/finally envelope."""
        await self.__aexit__(
            type(primary) if primary is not None else None,
            primary,
            primary.__traceback__ if primary is not None else None,
        )

    async def finish_active_exception(self) -> None:
        """Finish using the exception, if any, currently crossing ``finally``."""
        import sys

        await self.finish(sys.exception())

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        _traceback: object,
    ) -> bool:
        guard_error: BaseException | None = None
        guided_authority_lost = False
        if exc_value is None:
            fail_task = asyncio.create_task(
                self.service.fail_guided_operation(
                    self.lease.fence,
                    failure_code="operation_failed",
                    actor=_ACTOR,
                    session_operation_context=self.lease.session_operation_context,
                ),
                name="guided-operation-guard-prove-terminal",
            )
            try:
                await _join_shielded_task_after_cancellation(fail_task)
            except GuidedOperationFenceLostError:
                guided_authority_lost = True
            except BaseException as proof_error:
                guard_error = proof_error
            else:
                guard_error = AuditIntegrityError("Guided route returned before its guided operation became terminal")
        elif not isinstance(exc_value, GuidedOperationFenceLostError):
            fail_task = asyncio.create_task(
                self.service.fail_guided_operation(
                    self.lease.fence,
                    failure_code=_guided_failure_code_for_exception(exc_value),
                    actor=_ACTOR,
                    session_operation_context=self.lease.session_operation_context,
                ),
                name="guided-operation-guard-fail",
            )
            try:
                await _join_shielded_task_after_cancellation(fail_task)
            except GuidedOperationFenceLostError:
                guided_authority_lost = True
            except BaseException as cleanup_error:
                if cleanup_error is not exc_value:
                    exc_value.add_note(f"Guided-operation failure cleanup also failed with {type(cleanup_error).__name__}.")

        close_task = asyncio.create_task(self.lease.close(), name="guided-operation-guard-close")
        try:
            await _join_shielded_task_after_cancellation(close_task)
        except SessionOperationFenceLost as close_error:
            primary = exc_value or guard_error
            if primary is not None:
                primary.add_note(f"Session-operation guided cleanup also failed with {type(close_error).__name__}.")
            elif not guided_authority_lost:
                raise
        except BaseException as close_error:
            primary = exc_value or guard_error
            if primary is None:
                raise
            if close_error is not primary:
                primary.add_note(f"Session-operation guided cleanup also failed with {type(close_error).__name__}.")
        if guard_error is not None:
            raise guard_error from None
        return False


def guided_operation_lease_guard(
    *,
    service: SessionServiceProtocol,
    lease: GuidedOperationLease,
) -> _GuidedOperationLeaseGuard:
    """Encompass all post-claim work and close guided authority first on error."""
    return _GuidedOperationLeaseGuard(service=service, lease=lease)


@contextlib.asynccontextmanager
async def guided_operation_lock_guard(
    *,
    service: SessionServiceProtocol,
    lease: GuidedOperationLease,
    lock: asyncio.Lock,
) -> AsyncIterator[None]:
    """Enter cleanup authority before awaiting the route's compose lock."""
    async with guided_operation_lease_guard(service=service, lease=lease), lock:
        yield


@overload
async def reserve_or_replay_guided_operation[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: GuidedOperationKind,
    request: BaseModel,
    replay: Callable[[GuidedOperationResult], Awaitable[ResponseT]],
    reserve_if_absent: Literal[False],
    takeover_expired: Literal[False],
) -> GuidedOperationLease | GuidedOperationExpired | ResponseT | None: ...


@overload
async def reserve_or_replay_guided_operation[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: GuidedOperationKind,
    request: BaseModel,
    replay: Callable[[GuidedOperationResult], Awaitable[ResponseT]],
    reserve_if_absent: bool = True,
    takeover_expired: Literal[True] = True,
) -> GuidedOperationLease | ResponseT | None: ...


@overload
async def reserve_or_replay_guided_operation[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: GuidedOperationKind,
    request: BaseModel,
    replay: Callable[[GuidedOperationResult], Awaitable[ResponseT]],
    reserve_if_absent: Literal[True] = True,
    takeover_expired: Literal[False],
) -> Never: ...


async def reserve_or_replay_guided_operation[ResponseT: BaseModel](
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    kind: GuidedOperationKind,
    request: BaseModel,
    replay: Callable[[GuidedOperationResult], Awaitable[ResponseT]],
    reserve_if_absent: bool = True,
    takeover_expired: bool = True,
) -> GuidedOperationLease | GuidedOperationExpired | ResponseT | None:
    """Claim one operation or synchronously join its immutable terminal result.

    Active requests are polled to a terminal result.  Once a lease expires the
    caller returns to the atomic reserve primitive, which either performs the
    sole takeover or observes the competing taker's active/terminal outcome.
    The HTTP surface never exposes an intermediate 202 response.

    ``takeover_expired=False`` makes an existing expired operation return a
    distinct marker so a route can finish live preflight before acquiring a
    new fence. This mode is valid only with ``reserve_if_absent=False``.
    Non-expired active operations are still joined outside route state locks.
    """

    if not takeover_expired and reserve_if_absent:
        raise AuditIntegrityError("Non-taking-over guided operation lookup must not reserve an absent operation")

    operation_id = request.model_dump(mode="python").get("operation_id")
    if not isinstance(operation_id, str):
        raise AuditIntegrityError("Strict guided operation request has a non-string operation_id")
    request_hash = guided_operation_request_hash(session_id=session_id, kind=kind, request=request)

    async def reserve(session_lease: SessionOperationLease) -> GuidedOperationOutcome:
        try:
            return await service.reserve_guided_operation(
                session_id=session_id,
                operation_id=operation_id,
                kind=kind,
                request_hash=request_hash,
                actor=_ACTOR,
                lease_seconds=_LEASE_SECONDS,
                session_operation_context=session_lease.context,
            )
        except GuidedOperationConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="Operation id is already bound to a different request.",
            ) from exc

    try:
        existing = await service.get_guided_operation(
            session_id=session_id,
            operation_id=operation_id,
            kind=kind,
            request_hash=request_hash,
        )
    except GuidedOperationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="Operation id is already bound to a different request.",
        ) from exc
    outcome: GuidedOperationOutcome | None = existing
    observed_by_get = True
    session_lease: SessionOperationLease | None = None

    async def acquire_and_reserve() -> GuidedOperationOutcome:
        nonlocal session_lease
        operation_kind = SessionOperationKind.SESSION_FORK if kind == "session_fork" else SessionOperationKind.COMPOSE
        session_lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session_id,
            operation_kind=operation_kind,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        reserve_task = asyncio.create_task(reserve(session_lease), name="guided-operation-reserve")
        try:
            return await asyncio.shield(reserve_task)
        except asyncio.CancelledError as cancellation:
            try:
                cancellation_outcome = await _join_shielded_task_after_cancellation(reserve_task)
                if isinstance(cancellation_outcome, (GuidedOperationClaimed, GuidedOperationTakenOver)):
                    fail_task = asyncio.create_task(
                        service.fail_guided_operation(
                            cancellation_outcome.fence,
                            failure_code="request_cancelled",
                            actor=_ACTOR,
                            session_operation_context=session_lease.context,
                        ),
                        name="guided-operation-cancelled-reserve-fail",
                    )
                    await _join_shielded_task_after_cancellation(fail_task)
            except BaseException as cleanup_error:
                cancellation.add_note(f"Guided-operation reservation cancellation cleanup also failed with {type(cleanup_error).__name__}.")
            try:
                close_task = asyncio.create_task(session_lease.close(), name="guided-operation-cancelled-reserve-close")
                await _join_shielded_task_after_cancellation(close_task)
            except BaseException as close_error:
                cancellation.add_note(f"Session-operation reservation cancellation cleanup also failed with {type(close_error).__name__}.")
            session_lease = None
            raise cancellation from None
        except BaseException as primary:
            try:
                await session_lease.close()
            except BaseException as close_error:
                primary.add_note(f"Session-operation reservation cleanup also failed with {type(close_error).__name__}.")
            session_lease = None
            raise

    if outcome is None:
        if not reserve_if_absent:
            return None
        outcome = await acquire_and_reserve()
        observed_by_get = False
    while True:
        if isinstance(outcome, (GuidedOperationClaimed, GuidedOperationTakenOver)):
            if session_lease is None:
                raise AuditIntegrityError("Guided operation claim has no owning session lease")
            return GuidedOperationLease(fence=outcome.fence, session_lease=session_lease)
        if isinstance(outcome, GuidedOperationCompleted):
            if session_lease is not None:
                await session_lease.close()
                session_lease = None
            return await _replay_completed(outcome, replay)
        if isinstance(outcome, GuidedOperationFailed):
            if session_lease is not None:
                await session_lease.close()
                session_lease = None
            raise_guided_operation_failure(outcome)
        if not isinstance(outcome, GuidedOperationActive):
            raise AuditIntegrityError("Guided operation reserve returned an unknown outcome")

        if session_lease is not None:
            await session_lease.close()
            session_lease = None

        if outcome.expired and observed_by_get:
            if not takeover_expired:
                return GuidedOperationExpired(attempt=outcome.attempt)
            # ``expired`` is computed against the database clock by the read
            # primitive. Never compare the persisted timestamp with the web
            # host clock: even modest skew can create a tight reserve loop.
            outcome = await acquire_and_reserve()
            observed_by_get = False
            continue
        await asyncio.sleep(_POLL_SECONDS)
        try:
            observed = await service.get_guided_operation(
                session_id=session_id,
                operation_id=operation_id,
                kind=kind,
                request_hash=request_hash,
            )
        except GuidedOperationConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="Operation id is already bound to a different request.",
            ) from exc
        if observed is None:
            raise AuditIntegrityError("Guided operation disappeared while a caller was joining it")
        outcome = observed
        observed_by_get = True
