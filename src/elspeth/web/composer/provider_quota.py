"""Task-local quota custody around each audited Composer provider attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from typing import TYPE_CHECKING

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
    interrupted: asyncio.CancelledError | None = None
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError as error:
            # Keep waiting for the durable settlement, but retain cancellation
            # so the caller observes it after the ledger is reconciled.
            if settlement.done():
                raise
            interrupted = error
    settlement.result()
    span.attempt = None
    span.call = None
    if interrupted is not None:
        raise interrupted


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
            try:
                await _settle(span)
            finally:
                _SPAN.reset(token)

    return wrapped


async def admit_provider_attempt() -> None:
    """Commit pending evidence and admission before the physical dispatch."""
    if _CONTEXTVAR_GET(_SCOPE) is None:
        return
    span = _CONTEXTVAR_GET(_SPAN)
    if span is None:
        raise AuditIntegrityError("Composer provider dispatch lacks an audited quota span")
    # Retrying is another chargeable call. Settle the preceding attempt
    # before admission so one retry loop cannot run past a measured cap.
    await _settle(span)
    span.attempt = await span.scope.service.begin_provider_attempt(session_operation_context=span.scope.context, source="composer")


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
