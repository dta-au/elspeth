"""Task-local quota custody around each audited Composer provider attempt."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import IntEnum
from functools import wraps
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkRole, RequiredWorkSource, RequiredWorkTicket

if TYPE_CHECKING:
    from elspeth.web.sessions.protocol import SessionServiceProtocol


class ProviderInvocationFamily(IntEnum):
    PRIMARY = 0
    ADVISOR = 1
    PLANNER = 2
    TITLE = 3


class ProviderInvocationOwner:
    """Single explicit mint owner for independently ordered provider families."""

    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:
        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:
            raise AuditIntegrityError("Provider invocation owner requires its exact turn binding")
        required_work.validate_context(required_work.coordinator.authority.context)
        self._service = service
        self._required_work = required_work
        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)

    @property
    def service(self) -> SessionServiceProtocol:
        return self._service

    @property
    def required_work(self) -> RequiredWorkBinding:
        return self._required_work

    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:
        if type(family) is not ProviderInvocationFamily:
            raise AuditIntegrityError("Provider family is not an owned nominal identity")
        ordinal = self._counters[family]
        self._counters[family] = ordinal + 1
        diagonal = int(family) + ordinal
        semantic = diagonal * (diagonal + 1) // 2 + ordinal
        binding = RequiredWorkBinding(
            self.required_work.coordinator,
            self.required_work.transition_ordinal,
            semantic,
            RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN,
        )
        return ProviderCallCustody(service=self.service, context=binding.coordinator.authority.context, required_work=binding)


class ProviderCallCustody:
    """Explicit owned admission, physical dispatch and audit settlement custody."""

    __slots__ = (
        "__weakref__",
        "_admitting",
        "_context",
        "_required_work",
        "_sdk_entered",
        "_service",
        "_settlements",
        "_undispatched_recurrence",
        "attempt",
        "call",
    )

    def __init__(self, *, service: SessionServiceProtocol, context: SessionOperationContext, required_work: RequiredWorkBinding) -> None:
        if type(required_work) is not RequiredWorkBinding:
            raise AuditIntegrityError("Provider custody requires a nominal explicit binding")
        required_work.validate_context(context)
        if context.operation_kind is not SessionOperationKind.COMPOSE:
            raise AuditIntegrityError("Provider custody requires exact COMPOSE authority")
        self._service = service
        self._context = context
        self._required_work = required_work
        self.attempt: ProviderAttempt | None = None
        self.call: ComposerLLMCall | None = None
        self._sdk_entered = False
        self._admitting = False
        self._undispatched_recurrence = 0
        self._settlements = 0

    @property
    def service(self) -> SessionServiceProtocol:
        return self._service

    @property
    def context(self) -> SessionOperationContext:
        return self._context

    @property
    def required_work(self) -> RequiredWorkBinding:
        return self._required_work

    async def _join[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:
        task = asyncio.create_task(operation)
        cancellations: list[asyncio.CancelledError] = []
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as cancellation:
                if all(cancellation is not original for original in cancellations):
                    cancellations.append(cancellation)
            except BaseException:
                break
        if task.cancelled():
            try:
                task.result()
            except asyncio.CancelledError as child_cancellation:
                integrity = AuditIntegrityError("Required provider child lacks an actual delivered outcome")
                integrity.__cause__ = child_cancellation
                if cancellations:
                    raise BaseExceptionGroup(
                        "Provider self-cancelled child and original cancellations", [integrity, child_cancellation, *cancellations]
                    ) from None
                raise integrity from child_cancellation
        try:
            return task.result(), tuple(cancellations)
        except BaseException as failure:
            retained = failure
            if isinstance(failure, Exception) and not isinstance(
                failure,
                (
                    *contract_errors.TIER_1_ERRORS,
                    SQLAlchemyError,
                    SessionOperationFenceLost,
                    ChargeableAdmissionRefused,
                    BaseExceptionGroup,
                ),
            ):
                retained = ComposerOwnedSettlementFailure()
                retained.__cause__ = failure
            if cancellations:
                raise BaseExceptionGroup("Provider required child and cancellation outcomes", [retained, *cancellations]) from None
            if retained is not failure:
                raise retained from failure
            raise

    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:
        if len(cancellations) == 1:
            raise cancellations[0]
        if cancellations:
            raise BaseExceptionGroup("Provider original cancellations", list(cancellations))

    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:
        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:
            raise AuditIntegrityError("Title publication cannot precede provider accounting completion")
        return await self._join(operation)

    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:
        if self.attempt is None or self._sdk_entered:
            raise AuditIntegrityError("Undispatched disposition lacks exact no-SDK attempt evidence")
        binding = self.required_work
        ticket = binding.coordinator.reserve(
            RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
            transition_ordinal=binding.transition_ordinal,
            semantic_ordinal=binding.semantic_ordinal,
            recurrence_ordinal=self._undispatched_recurrence,
        )
        self._undispatched_recurrence += 1
        _, cancellations = await self._join(
            self.service.cancel_undispatched_provider_attempt(
                session_operation_context=self.context,
                attempt_id=self.attempt.attempt_id,
                requested_model=model,
                required_work=ticket,
            )
        )
        self.attempt = None
        self.call = None
        return cancellations

    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:
        """Dispose an actually admitted attempt whose physical SDK never began."""
        return await self._cancel_undispatched(model=model)

    async def admit(self, *, model: str) -> None:
        if type(model) is not str or not model or self._admitting:
            raise AuditIntegrityError("Provider admission lacks a single owned model invocation")
        self._admitting = True
        try:
            await self.settle()
            title = self.required_work.role is RequiredWorkRole.TITLE
            sql, projection = self.required_work.reserve_pair(
                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL,
                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
            )
            try:
                attempt, cancellations = await self._join(
                    self.service.begin_provider_attempt(
                        session_operation_context=self.context,
                        source="auto_title" if title else "composer",
                        required_work=sql,
                    )
                )
            except BaseException:
                if sql.complete:
                    projection.complete_without_submission()
                raise
            projection.begin_projection()
            try:
                if type(attempt) is not ProviderAttempt:
                    raise AuditIntegrityError("Provider admission returned a foreign attempt")
                self.attempt = attempt
                self.call = None
                self._sdk_entered = False
            except BaseException as failure:
                projection.complete_owned(failure)
                raise
            projection.complete_owned()
            if cancellations:
                additional = await self._cancel_undispatched(model=model)
                self.raise_deferred_cancellations((*cancellations, *additional))
        finally:
            self._admitting = False

    def mark_sdk_entered(self) -> None:
        if self.attempt is None or self._sdk_entered or self.call is not None:
            raise AuditIntegrityError("Physical provider dispatch lacks exact admitted custody")
        self._sdk_entered = True

    def needs_terminal_audit(self) -> bool:
        return self.attempt is not None and self._sdk_entered and self.call is None

    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:
        if type(call) is not ComposerLLMCall:
            raise AuditIntegrityError("Provider audit is not an owned complete call")
        if self.attempt is None or not self._sdk_entered:
            raise AuditIntegrityError("Provider audit lacks actual dispatched admission")
        bound = replace(call, call_id=self.attempt.attempt_id, started_at=self.attempt.started_at)
        self.call = bound
        return bound

    def retain_audit(self, call: ComposerLLMCall) -> bool:
        if type(call) is not ComposerLLMCall:
            raise AuditIntegrityError("Provider retained audit is not nominal")
        if self.attempt is None:
            return False
        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:
            raise AuditIntegrityError("Provider audit replaced actual admitted identity")
        self.call = call
        return True

    async def settle(self) -> None:
        if self.attempt is None:
            return
        if not self._sdk_entered or self.call is None:
            raise AuditIntegrityError("Required provider attempt lacks complete terminal audit")
        binding = self.required_work
        projection: RequiredWorkTicket | None = None
        if binding.role is RequiredWorkRole.TITLE:
            sql = binding.coordinator.reserve(
                RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
                transition_ordinal=binding.transition_ordinal,
                semantic_ordinal=binding.semantic_ordinal,
                recurrence_ordinal=self._settlements,
            )
            self._settlements += 1
        else:
            sql, projection = binding.reserve_pair(
                RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION
            )
        try:
            _, cancellations = await self._join(
                self.service.finish_provider_attempt(
                    session_operation_context=self.context,
                    call=self.call,
                    required_work=sql,
                )
            )
        except BaseException:
            if projection is not None and sql.complete:
                projection.complete_without_submission()
            raise
        if projection is not None:
            projection.begin_projection()
            projection.complete_owned()
        self.attempt = None
        self.call = None
        self._sdk_entered = False
        self.raise_deferred_cancellations(cancellations)


@contextmanager
def required_provider_audit_scope(custody: ProviderCallCustody | None) -> Iterator[None]:
    """Keep an original SDK/body outcome when owned audit construction fails."""
    original = sys.exception()
    try:
        yield
    except BaseException as audit_failure:
        if custody is not None and original is not None and audit_failure is not original:
            raise BaseExceptionGroup("Provider original body and audit construction outcomes", [original, audit_failure]) from None
        raise


@asynccontextmanager
async def provider_call_scope(custody: ProviderCallCustody) -> AsyncIterator[ProviderCallCustody]:
    if type(custody) is not ProviderCallCustody:
        raise AuditIntegrityError("Provider scope requires exact explicit custody")
    body: BaseException | None = None
    try:
        yield custody
    except BaseException as failure:
        body = failure
        raise
    finally:
        try:
            await custody.settle()
        except BaseException as settlement:
            if body is not None and settlement is not body:
                raise BaseExceptionGroup("Provider body and required settlement outcomes", [body, settlement]) from None
            raise


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
        if isinstance(failure, (ChargeableAdmissionRefused, SessionOperationFenceLost, SQLAlchemyError)):
            raise cancellation from failure
        raise ComposerOwnedSettlementFailure() from failure


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
            except ComposerOwnedSettlementFailure as failure:
                raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from failure
            except asyncio.CancelledError as interrupted:
                if interrupted.__cause__ is not None:
                    raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from interrupted.__cause__
                raise
        except BaseException as failure:
            if isinstance(failure, contract_errors.TIER_1_ERRORS):
                raise failure from cancellation
            raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from failure
        raise cancellation
    except (ChargeableAdmissionRefused, SessionOperationFenceLost, SQLAlchemyError):
        raise
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as failure:
        raise ComposerOwnedSettlementFailure() from failure


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
