"""Task-local quota custody around each audited Composer provider attempt."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from typing import TYPE_CHECKING

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind

if TYPE_CHECKING:
    from elspeth.web.coordination.quota_authority import ProviderAttempt
    from elspeth.web.sessions.protocol import SessionServiceProtocol


@dataclass(frozen=True, slots=True)
class _QuotaScope:
    service: SessionServiceProtocol
    context: SessionOperationContext


@dataclass(slots=True)
class _ProviderSpan:
    scope: _QuotaScope
    attempt: ProviderAttempt | None = None
    call: ComposerLLMCall | None = None


_SCOPE: ContextVar[_QuotaScope | None] = ContextVar("composer_quota_scope", default=None)
_SPAN: ContextVar[_ProviderSpan | None] = ContextVar("composer_provider_span", default=None)
_CONTEXTVAR_GET = ContextVar.get


@contextmanager
def composer_quota_scope(service: SessionServiceProtocol, context: SessionOperationContext | None) -> Iterator[None]:
    """Bind session authority for provider work, restoring it on every exit."""
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise AuditIntegrityError("Composer quota scope requires an exact COMPOSE operation context")
    token = _SCOPE.set(_QuotaScope(service, context))
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def _settle(span: _ProviderSpan) -> None:
    if span.attempt is None:
        return
    if span.call is None:
        # The pending row remains unknown. Never invent a terminal verdict
        # when the owning audit path did not produce one.
        raise AuditIntegrityError("Composer provider attempt has no terminal audit evidence")
    settlement = asyncio.create_task(
        span.scope.service.finish_provider_attempt(session_operation_context=span.scope.context, call=span.call)
    )
    try:
        await asyncio.shield(settlement)
    except asyncio.CancelledError as cancellation:
        # The shielded write can outlive repeated caller cancellation. Observe
        # its actual outcome before releasing the operation's audit authority.
        while not settlement.done():
            with suppress(BaseException):
                await asyncio.shield(settlement)
        if settlement.cancelled():
            raise AuditIntegrityError("Composer provider settlement was cancelled before durable completion") from cancellation
        failure = settlement.exception()
        if failure is not None:
            if isinstance(failure, contract_errors.TIER_1_ERRORS):
                raise failure from cancellation
            raise cancellation from failure
        span.attempt = None
        span.call = None
        raise cancellation
    settlement.result()
    span.attempt = None
    span.call = None


def quota_provider_calls[**P, R](operation: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Enclose an audited provider operation, including its retry attempts."""

    @wraps(operation)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        scope = _CONTEXTVAR_GET(_SCOPE)
        if scope is None:
            return await operation(*args, **kwargs)
        span = _ProviderSpan(scope)
        token = _SPAN.set(span)
        try:
            return await operation(*args, **kwargs)
        finally:
            primary = sys.exception()
            try:
                try:
                    await _settle(span)
                except BaseException as settlement_error:
                    if isinstance(primary, asyncio.CancelledError) and settlement_error is not primary:
                        if isinstance(settlement_error, asyncio.CancelledError):
                            child_failure = settlement_error.__cause__
                            if child_failure is None:
                                raise primary from None
                            settlement_error = child_failure
                        if isinstance(settlement_error, contract_errors.TIER_1_ERRORS):
                            raise settlement_error from primary
                        raise primary from settlement_error
                    raise
            finally:
                _SPAN.reset(token)

    return wrapped


async def _join_after_cancellation[R](task: asyncio.Task[R], cancellation: asyncio.CancelledError) -> R:
    """Observe an owned task after cancellation without losing its result."""
    while not task.done():
        with suppress(BaseException):
            await asyncio.shield(task)
    if task.cancelled():
        raise AuditIntegrityError("Composer provider custody task cancelled before its outcome was known") from cancellation
    try:
        return task.result()
    except BaseException as failure:
        if isinstance(failure, contract_errors.TIER_1_ERRORS):
            raise failure from cancellation
        raise cancellation from failure


async def admit_provider_attempt(*, model: str) -> None:
    """Commit pending evidence and admission before the physical dispatch."""
    if _CONTEXTVAR_GET(_SCOPE) is None:
        return
    span = _CONTEXTVAR_GET(_SPAN)
    if span is None:
        raise AuditIntegrityError("Composer provider dispatch lacks an audited quota span")
    if type(model) is not str or not model:
        raise AuditIntegrityError("Composer provider attempt requires an owned requested model")
    # Retrying is another chargeable call. Settle the preceding attempt
    # before admission so one retry loop cannot run past a measured cap.
    await _settle(span)
    admission = asyncio.create_task(
        span.scope.service.begin_provider_attempt(session_operation_context=span.scope.context, source="composer")
    )
    try:
        span.attempt = await asyncio.shield(admission)
    except asyncio.CancelledError as cancellation:
        attempt = await _join_after_cancellation(admission, cancellation)
        # The SDK has not been entered: only this narrow path can assert zero
        # usage without inventing a provider call. The authority records that
        # explicit disposition under the original admission fence.
        disposition = asyncio.create_task(
            span.scope.service.cancel_undispatched_provider_attempt(
                session_operation_context=span.scope.context,
                attempt_id=attempt.attempt_id,
                requested_model=model,
            )
        )
        try:
            await asyncio.shield(disposition)
        except asyncio.CancelledError:
            try:
                await _join_after_cancellation(disposition, cancellation)
            except asyncio.CancelledError as interrupted:
                if interrupted.__cause__ is not None:
                    raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from interrupted.__cause__
                raise
        except BaseException as failure:
            if isinstance(failure, contract_errors.TIER_1_ERRORS):
                raise failure from cancellation
            raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from failure
        raise cancellation


def provider_attempt_needs_terminal_audit() -> bool:
    """Whether a completion failure belongs to an admitted, unaudited attempt.

    Retry admission first settles the preceding call. If that fails, the
    span still holds its terminal evidence and must not be rebound to the
    retry's exception. Unscoped callers retain their own audit lifecycle.
    Check before building a call record, which binds it into the span.
    """
    span = _CONTEXTVAR_GET(_SPAN)
    return span is None or (span.attempt is not None and span.call is None)


def bind_provider_attempt(call: ComposerLLMCall) -> ComposerLLMCall:
    """Use the database-clock identity of the actual dispatched attempt."""
    span = _CONTEXTVAR_GET(_SPAN)
    if span is None or span.attempt is None:
        return call
    bound = replace(call, call_id=span.attempt.attempt_id, started_at=span.attempt.started_at)
    span.call = bound
    return bound


def retain_provider_audit(call: ComposerLLMCall) -> bool:
    """Retain the final classification after caller validation of a response."""
    span = _CONTEXTVAR_GET(_SPAN)
    if span is not None:
        if span.attempt is None:
            # Admission failed before dispatch. The surrounding exception
            # remains authoritative; there was no provider call to audit.
            return False
        if call.call_id != span.attempt.attempt_id:
            raise AuditIntegrityError("Composer terminal audit does not identify its provider attempt")
        span.call = call
    return True
