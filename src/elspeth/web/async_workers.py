"""Async helpers for running bounded synchronous work off the event loop."""

from __future__ import annotations

import asyncio
import functools
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from types import MethodType
from typing import Any, Final

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.application_finalizers import _FINALIZER_PHYSICAL_ISSUER_SEAL, ApplicationFinalizerCapability, ApplicationFinalizerOwner
from elspeth.web.composer_watch_reads import _WATCH_REFUSAL_ISSUER_SEAL, ComposerOperationWatchReader
from elspeth.web.execution_lease_cleanup import _EXECUTION_SQL_BRIDGE_ISSUER_SEAL, ExecutionLeaseSQLSubmission
from elspeth.web.required_executor import (
    InvocationGate,
    InvocationReservation,
    RequiredExecutorGenerationCustodian,
    RequiredGenerationUnavailable,
    RequiredInvocationWitness,
)
from elspeth.web.required_executor import (
    RequiredGenerationDrainExpired as RequiredGenerationDrainExpired,
)
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkTicket

#: Threads in the process-wide worker pool.
MAX_WORKERS: Final[int] = 16
#: Submissions allowed to sit queued behind busy threads. Together with
#: ``MAX_WORKERS`` this bounds outstanding admissions (running + queued);
#: everything past it is rejected fast rather than queued for nobody.
MAX_QUEUED: Final[int] = 16
ADMISSION_CAPACITY: Final[int] = MAX_WORKERS + MAX_QUEUED
#: How long a caller waits for an admission slot before it is rejected.
#: Healthy work is millisecond-scale, so this only bites once the pool is
#: saturated by work that is not finishing — the case where waiting longer
#: cannot help and hanging the caller hides the fault.
ADMISSION_WAIT_SECONDS: Final[float] = 1.0
_ADMISSION_POLL_SECONDS: Final[float] = 0.02

_SHARED_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
# Outstanding admissions: submissions whose sync work has not yet finished
# (queued or running). Guarded by ``_EXECUTOR_LOCK`` because it is released
# from worker threads, and process-wide because the pool is process-wide.
_OUTSTANDING_ADMISSIONS = 0


_SHARED_SHUTDOWN_STARTED = False
_GENERATION_COUNTER = 0
_GENERATION_CUSTODIAN: RequiredExecutorGenerationCustodian | None = None
_GENERATION_UNAVAILABLE = threading.Event()
_INSTANCE_DRAINING = threading.Event()
_APPLICATION_FINALIZER_OWNER: ApplicationFinalizerOwner | None = None
_RECOVERY_CALLBACK: Callable[[RequiredGenerationDrainExpired], None] | None = None
_CAPTURED_DRAIN_SECONDS = 10.0


def configure_required_executor_recovery(
    *,
    drain_seconds: float,
    instance_draining: threading.Event,
    generation_unavailable: threading.Event,
    recovery_callback: Callable[[RequiredGenerationDrainExpired], None],
    application_finalizer_owner: ApplicationFinalizerOwner | None = None,
) -> None:
    """Register explicit lifecycle ownership before the first shared submission."""
    global \
        _RECOVERY_CALLBACK, \
        _INSTANCE_DRAINING, \
        _GENERATION_UNAVAILABLE, \
        _CAPTURED_DRAIN_SECONDS, \
        _SHARED_SHUTDOWN_STARTED, \
        _APPLICATION_FINALIZER_OWNER
    if isinstance(drain_seconds, bool) or not math.isfinite(drain_seconds) or drain_seconds <= 0:
        raise ValueError("Required generation drain must be positive")
    if _GENERATION_CUSTODIAN is not None and (
        _GENERATION_CUSTODIAN.escalated
        or (_GENERATION_CUSTODIAN.state == "quarantined" and not _GENERATION_CUSTODIAN.recovery_finished.is_set())
    ):
        raise RequiredGenerationUnavailable("Existing generation recovery cannot be replaced")
    if _SHARED_SHUTDOWN_STARTED and _SHARED_EXECUTOR is not None:
        raise RequiredGenerationUnavailable("Previous shared generation has not joined")
    _SHARED_SHUTDOWN_STARTED = False
    _APPLICATION_FINALIZER_OWNER = application_finalizer_owner
    _RECOVERY_CALLBACK = recovery_callback
    _INSTANCE_DRAINING = instance_draining
    _GENERATION_UNAVAILABLE = generation_unavailable
    _CAPTURED_DRAIN_SECONDS = drain_seconds


def _assert_recovery_registered() -> None:
    if _RECOVERY_CALLBACK is None:
        raise AuditIntegrityError("Shared executor recovery callback absent")


def required_generation_unavailable() -> bool:
    _assert_recovery_registered()
    return _GENERATION_UNAVAILABLE.is_set() or _INSTANCE_DRAINING.is_set()


def _install_replacement(executor: ThreadPoolExecutor) -> None:
    global _SHARED_EXECUTOR
    with _EXECUTOR_LOCK:
        _SHARED_EXECUTOR = executor


def _submission_unavailable(ticket: RequiredWorkTicket | None, finalizer: ApplicationFinalizerCapability | None = None) -> bool:
    _assert_recovery_registered()
    if finalizer is not None and (_APPLICATION_FINALIZER_OWNER is None or not _APPLICATION_FINALIZER_OWNER.owns_claimed(finalizer)):
        raise AuditIntegrityError("Invalid application finalizer authority")
    allowed_finalizer = (
        finalizer is not None and _APPLICATION_FINALIZER_OWNER is not None and _APPLICATION_FINALIZER_OWNER.owns_claimed(finalizer)
    )
    return _GENERATION_UNAVAILABLE.is_set() or (
        _INSTANCE_DRAINING.is_set() and not allowed_finalizer and (ticket is None or not ticket.permits_process_drain)
    )


def _generation_for(
    executor: ThreadPoolExecutor, ticket: RequiredWorkTicket | None = None, finalizer: ApplicationFinalizerCapability | None = None
) -> RequiredExecutorGenerationCustodian:
    global _GENERATION_COUNTER, _GENERATION_CUSTODIAN
    if _submission_unavailable(ticket, finalizer):
        raise RequiredGenerationUnavailable("Shared executor lifecycle unavailable")
    callback = _RECOVERY_CALLBACK
    if callback is None:
        raise AuditIntegrityError("Shared executor recovery callback absent")
    with _EXECUTOR_LOCK:
        if _GENERATION_CUSTODIAN is None or _GENERATION_CUSTODIAN.executor is not executor:
            _GENERATION_COUNTER += 1
            _GENERATION_CUSTODIAN = RequiredExecutorGenerationCustodian(
                executor,
                generation_identity=_GENERATION_COUNTER,
                drain_seconds=_CAPTURED_DRAIN_SECONDS,
                generation_unavailable=_GENERATION_UNAVAILABLE,
                instance_draining=_INSTANCE_DRAINING,
                recovery_callback=callback,
                replacement_factory=lambda: ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="async-worker"),
                install_replacement=_install_replacement,
            )
        return _GENERATION_CUSTODIAN


def _owned_setup_failure(error: BaseException) -> BaseException:
    if isinstance(error, (AuditIntegrityError, SQLAlchemyError, AsyncWorkerAdmissionTimeoutError)):
        return error
    marker = ComposerOwnedSettlementFailure()
    marker.__cause__ = error
    return marker


async def _submit_shared[T](
    callable_: Callable[[], T],
    ticket: RequiredWorkTicket | None = None,
    finalizer: ApplicationFinalizerCapability | None = None,
    *,
    deferred_cancellations: list[asyncio.CancelledError] | None = None,
    watch_reader: ComposerOperationWatchReader | None = None,
) -> ConcurrentFuture[T]:
    if watch_reader is not None and (
        type(watch_reader) is not ComposerOperationWatchReader
        or ticket is not None
        or finalizer is not None
        or type(callable_) is not MethodType
        or callable_.__self__ is not watch_reader
        or callable_.__func__ is not ComposerOperationWatchReader.invoke
    ):
        raise AuditIntegrityError("Operation observation dispatch is not its prebound read")
    if finalizer is not None and (
        type(finalizer) is not ApplicationFinalizerCapability
        or type(callable_) is not MethodType
        or callable_.__self__ is not finalizer
        or callable_.__func__ is not ApplicationFinalizerCapability.invoke
    ):
        raise AuditIntegrityError("Application finalizer dispatch changed its prebound invocation")
    loop = asyncio.get_running_loop()
    try:
        executor = _get_shared_executor()
        generation = _generation_for(executor, ticket, finalizer)
        if watch_reader is not None:
            watch_reader.pin_generation(executor, generation)
    except BaseException as pre_submission_failure:
        if ticket is not None:
            ticket.complete_without_submission(pre_submission_failure)
        raise
    finalizer_reservation_bound = False

    def release_actual_admission() -> None:
        _release_admission()
        if finalizer is not None and finalizer_reservation_bound:
            ApplicationFinalizerCapability._observe_physical_admission_release_return(
                finalizer, _FINALIZER_PHYSICAL_ISSUER_SEAL, reservation
            )

    reservation = InvocationReservation(
        RequiredInvocationWitness(generation.identity),
        release_actual_admission if finalizer is not None else _release_admission,
        ticket,
    )
    try:
        if watch_reader is None:
            await _acquire_admission()
        else:
            refusal = await _acquire_watch_admission(watch_reader)
            if refusal is not None:
                watch_reader.retain_refusal_original(refusal)
                with generation.submission_lock:
                    _assert_current_watch_generation(watch_reader, executor, generation)
                    watch_reader.issue_refusal(_WATCH_REFUSAL_ISSUER_SEAL, refusal, executor, generation, reservation)
                raise refusal
    except BaseException as exc:
        if ticket is not None:
            ticket.complete_without_submission(exc)
        raise
    reservation.held = True
    submission_failure: BaseException | None = None
    future: ConcurrentFuture[T] | None = None
    with generation.submission_lock:
        try:
            if generation.state != "active" or _submission_unavailable(ticket, finalizer):
                raise RequiredGenerationUnavailable("Shared executor generation quarantined")
            if watch_reader is not None:
                _assert_current_watch_generation(watch_reader, executor, generation)
        except BaseException as failure:
            reservation.release_unused()
            if ticket is not None:
                ticket.complete_without_submission(failure)
            raise
        generation.register(reservation)
        try:
            if finalizer is not None:
                finalizer._bind_physical_reservation(_FINALIZER_PHYSICAL_ISSUER_SEAL, reservation)
                finalizer_reservation_bound = True
            future = executor.submit(reservation.witness.invoke, callable_)
        except BaseException as exc:
            submission_failure = exc
            reservation.submission_error = exc
            reservation.witness.abort()
            if ticket is not None:
                ticket.observe_submission_unknown(exc)
                ticket.observe_custody_failure(_owned_setup_failure(exc))
            generation.quarantine()
        else:
            reservation.future = future
            try:
                if finalizer is not None:
                    finalizer._bind_physical_future(_FINALIZER_PHYSICAL_ISSUER_SEAL, future)
                if ticket is not None:
                    ticket.bind_future(future)
                if finalizer is None:
                    future.add_done_callback(reservation.release_future)
                else:
                    owned_finalizer = finalizer

                    def release_finalizer_actual(done: ConcurrentFuture[T]) -> None:
                        try:
                            reservation.release_future(done)
                            ApplicationFinalizerCapability._observe_physical_callback_return(
                                owned_finalizer, _FINALIZER_PHYSICAL_ISSUER_SEAL, reservation
                            )
                        except BaseException as original:
                            reservation.semantic_observation_error = original
                            owned_finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, original)
                            try:
                                loop.call_soon_threadsafe(generation.quarantine)
                            except BaseException as scheduling_original:
                                reservation.semantic_observation_error = scheduling_original
                                owned_finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, scheduling_original)
                            ApplicationFinalizerCapability._observe_physical_callback_failure_exit(
                                owned_finalizer, _FINALIZER_PHYSICAL_ISSUER_SEAL, reservation
                            )

                    future.add_done_callback(release_finalizer_actual)
                reservation.witness.arm()
            except BaseException as exc:
                submission_failure = exc
                reservation.setup_error = exc
                if ticket is not None:
                    ticket.observe_custody_failure(_owned_setup_failure(exc))
                if finalizer is None:
                    reservation.witness.abort()
                else:
                    finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, exc)
                    if reservation.witness.snapshot().gate is InvocationGate.WAITING:
                        try:
                            reservation.witness.abort()
                        except BaseException as abort_original:
                            reservation.semantic_observation_error = abort_original
                            finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, abort_original)
                generation.quarantine()
    if submission_failure is not None:
        if watch_reader is not None:
            watch_reader.retain_physical_failure_original(submission_failure)
        if finalizer is not None:
            finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, submission_failure)
        while not finalizer.physical_submission_failure_completion_known if finalizer is not None else not reservation.released:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as cancellation:
                # Rare setup failure remains owned until its actual proof.
                if deferred_cancellations is not None:
                    _retain_cancellation(deferred_cancellations, cancellation)
                continue
        if future is not None:
            while not future.done():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError as cancellation:
                    if deferred_cancellations is not None:
                        _retain_cancellation(deferred_cancellations, cancellation)
                    continue
            try:
                future.result()
            except BaseException as actual_error:
                reservation.setup_outcome_error = actual_error
                if finalizer is not None:
                    finalizer._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, actual_error)
        raise submission_failure
    if future is None:
        raise RuntimeError("Submission completed without an actual Future")
    return future


def _raise_lifecycle_originals(cancellations: list[asyncio.CancelledError], failures: list[BaseException]) -> None:
    originals: list[BaseException] = []
    for original in (*cancellations, *failures):
        if all(original is not retained for retained in originals):
            originals.append(original)
    if len(originals) == 1:
        raise originals[0]
    if originals:
        raise BaseExceptionGroup("Application lifecycle retained original outcomes", originals)


async def run_application_finalizer_in_worker(capability: ApplicationFinalizerCapability) -> object:
    owner = _APPLICATION_FINALIZER_OWNER
    if owner is None or not _INSTANCE_DRAINING.is_set():
        raise AuditIntegrityError("Application finalizer owner absent or lifecycle not draining")
    owner.claim(capability)
    cancellations: list[asyncio.CancelledError] = []
    try:
        future = await _submit_shared(capability.invoke, finalizer=capability, deferred_cancellations=cancellations)
    except BaseException as original:
        _raise_lifecycle_originals(cancellations, [original, *capability.physical_failure_originals])
        raise
    while not future.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            _retain_cancellation(cancellations, original)
    while not capability.physical_completion_known and not capability.physical_failure_completion_known:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            _retain_cancellation(cancellations, original)
    try:
        value = future.result()
    except BaseException as original:
        capability._retain_physical_failure(_FINALIZER_PHYSICAL_ISSUER_SEAL, original)
        _raise_lifecycle_originals(cancellations, [original, *capability.physical_failure_originals])
        raise
    _raise_lifecycle_originals(cancellations, list(capability.physical_failure_originals))
    return value


async def run_required_sql_in_worker[**P, T](ticket: RequiredWorkTicket, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Finish submitted required SQL and retain its exact actual Future outcome."""
    if not isinstance(ticket, RequiredWorkTicket):
        raise TypeError("Required SQL needs an owned ticket")
    outcome = await run_required_sql_finish_once(ticket, func, *args, **kwargs)
    cancellations = list(outcome.deferred_cancellations)
    if isinstance(outcome, RequiredSQLRaised):
        _raise_lifecycle_originals(cancellations, [outcome.error])
        raise outcome.error
    _raise_lifecycle_originals(cancellations, [])
    return outcome.value


def _retain_cancellation(cancellations: list[asyncio.CancelledError], cancellation: asyncio.CancelledError) -> None:
    if all(cancellation is not earlier for earlier in cancellations):
        cancellations.append(cancellation)


def _validate_finish_once_ticket(ticket: RequiredWorkTicket) -> None:
    if not isinstance(ticket, RequiredWorkTicket):
        raise TypeError("Required SQL needs an owned ticket")
    if ticket.complete:
        raise AuditIntegrityError("Completed required SQL ticket cannot be reused")


async def run_required_sql_finish_once[**P, T](
    ticket: RequiredWorkTicket, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> RequiredSQLFinishOnce[T]:
    """Hand off a known actual SQL outcome without discarding it on cancellation."""
    _validate_finish_once_ticket(ticket)
    cancellations: list[asyncio.CancelledError] = []
    try:
        future = await _submit_shared(functools.partial(func, *args, **kwargs), ticket, deferred_cancellations=cancellations)
    except BaseException as error:
        if not ticket.complete:
            # Unknown physical custody is never a successful service handoff.
            raise
        if isinstance(error, asyncio.CancelledError):
            _retain_cancellation(cancellations, error)
        outcome = RequiredSQLRaised(error, tuple(cancellations))
        ticket.observe_finish_once_handoff(outcome)
        return outcome
    while not future.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as cancellation:
            _retain_cancellation(cancellations, cancellation)
    try:
        value = future.result()
    except BaseException as error:
        ticket.observe_actual_outcome()
        outcome = RequiredSQLRaised(error, tuple(cancellations))
        ticket.observe_finish_once_handoff(outcome)
        return outcome
    ticket.observe_actual_outcome()
    returned = RequiredSQLReturned(value, tuple(cancellations))
    ticket.observe_finish_once_handoff(returned)
    return returned


AUTH_AUDIT_MAX_WORKERS: Final[int] = 2
AUTH_AUDIT_ADMISSION_CAPACITY: Final[int] = 4


class _AuthAuditWorkers:
    """Reserved capacity for must-fire refusals when ordinary workers are full."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.executor: ThreadPoolExecutor | None = None
        self.outstanding = 0

    def get_executor(self) -> ThreadPoolExecutor:
        with self.lock:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=AUTH_AUDIT_MAX_WORKERS, thread_name_prefix="auth-audit")
            return self.executor

    def try_admit(self) -> bool:
        with self.lock:
            if self.outstanding >= AUTH_AUDIT_ADMISSION_CAPACITY:
                return False
            self.outstanding += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.outstanding -= 1

    def release_when_finished(self, _finished: ConcurrentFuture[Any]) -> None:
        self.release()

    def detach_executor(self) -> ThreadPoolExecutor | None:
        with self.lock:
            executor = self.executor
            self.executor = None
            return executor


_AUTH_AUDIT_WORKERS = _AuthAuditWorkers()


class AsyncWorkerAdmissionTimeoutError(TimeoutError):
    """The shared worker pool could not admit new work within the bounded wait.

    Raised instead of queueing indefinitely once ``ADMISSION_CAPACITY``
    submissions are outstanding — typically because earlier workers are hung
    and their callers have already timed out (elspeth-5269b43bca). It is a
    ``TimeoutError`` on purpose: every deadline-bounded caller already
    classifies a bounded wait that expired as its own TIMEOUT outcome.
    """


def _get_shared_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded worker pool, constructing it once.

    ``run_sync_in_worker`` previously built a fresh ``ThreadPoolExecutor`` on
    every call, so N concurrent callers spawned N unbounded threads. A single
    bounded pool caps that at ``max_workers``. It is safe against re-entrant
    deadlock because ``run_sync_in_worker`` requires a running event loop and
    therefore cannot be called from inside a worker thread.
    """
    global _SHARED_EXECUTOR
    _assert_recovery_registered()
    if _SHARED_SHUTDOWN_STARTED:
        raise RequiredGenerationUnavailable("Shared executor shutdown has started")
    if _SHARED_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _SHARED_EXECUTOR is None:
                _SHARED_EXECUTOR = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS,
                    thread_name_prefix="async-worker",
                )
    return _SHARED_EXECUTOR


async def shutdown_async_workers() -> None:
    """Join every actual shutdown Future and retain every original outcome."""
    global _SHARED_EXECUTOR, _SHARED_SHUTDOWN_STARTED
    _SHARED_SHUTDOWN_STARTED = True
    executors: list[ThreadPoolExecutor] = []
    shared = _SHARED_EXECUTOR
    generation = _GENERATION_CUSTODIAN
    if generation is not None:
        with generation.submission_lock:
            if generation.state != "quarantined":
                generation.state = "closed"
    cancellations: list[asyncio.CancelledError] = []
    failures: list[BaseException] = []
    shared_joined = shared is None
    if generation is not None and generation.executor is shared and generation.state == "quarantined":
        while not generation.joined.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as original:
                _retain_cancellation(cancellations, original)
        shared_joined = True
        if generation.drain_error is not None:
            failures.append(generation.drain_error)
    elif shared is not None:
        executors.append(shared)
    audit_executor = _AUTH_AUDIT_WORKERS.detach_executor()
    if audit_executor is not None:
        executors.append(audit_executor)
    if executors:
        shutdown_pool = ThreadPoolExecutor(max_workers=len(executors), thread_name_prefix="async-worker-shutdown")
        futures: list[tuple[ThreadPoolExecutor, ConcurrentFuture[None]]] = []
        try:
            for executor in executors:
                try:
                    future = shutdown_pool.submit(executor.shutdown, wait=True, cancel_futures=False)
                except BaseException as original:
                    failures.append(original)
                else:
                    futures.append((executor, future))
            for executor, future in futures:
                while not future.done():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as original:
                        _retain_cancellation(cancellations, original)
                try:
                    future.result()
                except BaseException as original:
                    failures.append(original)
                else:
                    if executor is shared:
                        shared_joined = True
        finally:
            try:
                shutdown_pool.shutdown(wait=True, cancel_futures=False)
            except BaseException as original:
                failures.append(original)
    if shared_joined and not failures:
        _SHARED_EXECUTOR = None
        if not _INSTANCE_DRAINING.is_set():
            _SHARED_SHUTDOWN_STARTED = False
    _raise_lifecycle_originals(cancellations, failures)


def outstanding_admissions() -> int:
    """Return submissions whose sync work has not finished (queued + running)."""
    with _EXECUTOR_LOCK:
        return _OUTSTANDING_ADMISSIONS


def _try_admit() -> bool:
    global _OUTSTANDING_ADMISSIONS
    with _EXECUTOR_LOCK:
        if _OUTSTANDING_ADMISSIONS >= ADMISSION_CAPACITY:
            return False
        _OUTSTANDING_ADMISSIONS += 1
        return True


def _release_admission() -> None:
    global _OUTSTANDING_ADMISSIONS
    with _EXECUTOR_LOCK:
        _OUTSTANDING_ADMISSIONS -= 1


def _release_admission_when_finished(_finished: ConcurrentFuture[Any]) -> None:
    """Free one admission slot when the sync work actually finishes.

    Attached to the *concurrent* future so it fires on the worker thread at
    completion — or immediately when a still-queued submission is cancelled —
    regardless of whether the awaiting event loop is still alive. Admission
    therefore follows the worker's lifetime, never the awaiter's.
    """
    _release_admission()


async def _acquire_admission() -> None:
    """Wait a bounded time for an admission slot, then reject.

    Polls rather than binding an ``asyncio`` primitive to one loop: the
    admission count is process-wide (the pool is process-wide, and slots are
    released from worker threads), and this helper must stay usable from any
    running loop.
    """
    await _wait_for_admission(_try_admit, ADMISSION_CAPACITY, "async worker")


async def _wait_for_admission(try_admit: Callable[[], bool], capacity: int, pool_name: str) -> None:
    if try_admit():
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMISSION_WAIT_SECONDS
    while True:
        await asyncio.sleep(_ADMISSION_POLL_SECONDS)
        if try_admit():
            return
        if loop.time() >= deadline:
            raise AsyncWorkerAdmissionTimeoutError(
                f"{pool_name} pool saturated: {capacity} submissions outstanding for {ADMISSION_WAIT_SECONDS}s"
            )


async def run_sync_in_worker[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run synchronous work on the shared bounded worker pool without blocking the loop.

    Admission is bounded through the WORKER's lifetime (elspeth-5269b43bca):
    a caller that times out or is cancelled does not free the slot its sync
    work still occupies, so a retry storm against hung workers stops at
    ``ADMISSION_CAPACITY`` with a fast ``AsyncWorkerAdmissionTimeoutError``
    instead of queueing behind them indefinitely and starving unrelated
    worker-backed calls.

    The short wait loop keeps an explicit event-loop timer active while the
    worker runs.  This preserves the normal async-over-sync contract even in
    sandboxed runtimes where executor completion can fail to wake the selector
    promptly.
    """
    concurrent_future = await _submit_shared(functools.partial(func, *args, **kwargs))
    return await _await_worker_future(concurrent_future, cancel_queued=True)


async def run_stream_read_in_worker[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Keep a stream read's awaiter alive until its actual shared-pool work ends.

    A cancelled queued future is done without running. A running thread cannot
    be cancelled; its task joins completion despite repeated cancellation so
    the subscriber can retain capacity through the real SQL lifetime.
    """
    concurrent_future = await _submit_shared(functools.partial(func, *args, **kwargs))
    cancelled = False
    while not concurrent_future.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled = True
            concurrent_future.cancel()
    if cancelled:
        if not concurrent_future.cancelled():
            try:
                concurrent_future.result()
            except BaseException as exc:
                raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_auth_audit_in_worker[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Record a refusal off-loop using capacity reserved from ordinary work.

    At most two audits run and two wait. Admission failure propagates: an
    unaudited refusal must not be reported as successfully audited. Once
    admitted, audit work survives caller cancellation even while queued.
    Its slot is released by the concurrent future only when work finishes.
    """
    executor = _AUTH_AUDIT_WORKERS.get_executor()
    await _wait_for_admission(_AUTH_AUDIT_WORKERS.try_admit, AUTH_AUDIT_ADMISSION_CAPACITY, "auth audit worker")
    try:
        concurrent_future = executor.submit(functools.partial(func, *args, **kwargs))
    except BaseException:
        _AUTH_AUDIT_WORKERS.release()
        raise
    concurrent_future.add_done_callback(_AUTH_AUDIT_WORKERS.release_when_finished)
    return await _await_worker_future(concurrent_future, cancel_queued=False)


def _retrieve_abandoned_exception(future: asyncio.Future[Any]) -> None:
    """Consume the outcome after the audit caller has disconnected."""
    if not future.cancelled():
        future.exception()


async def _await_worker_future[T](concurrent_future: ConcurrentFuture[T], *, cancel_queued: bool) -> T:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        # ``asyncio.wait`` returns ``(done, pending)`` on each 0.1s tick rather
        # than raising on timeout, so there is no error-shaped sentinel to
        # catch — the empty ``done`` set is the explicit "not finished yet"
        # signal and we simply loop again. The short timeout keeps an explicit
        # event-loop timer active so the selector wakes promptly even in
        # sandboxed runtimes. Unlike ``wait_for(shield(...))``, ``asyncio.wait``
        # never cancels the future it is waiting on, so no ``shield`` wrapper is
        # needed to keep the worker alive across a caller cancellation.
        while True:
            done, _pending = await asyncio.wait({future}, timeout=0.1)
            if done:
                # ``future.result()`` retrieves the value or re-raises the
                # worker's exception on the awaited (non-cancelled) path.
                return future.result()
    finally:
        # For ordinary work abandoned mid-flight (cancellation, outer
        # timeout), cancel the wrapper:
        #
        # * still QUEUED — the concurrent future is cancelled too, so work
        #   nobody will read never occupies a thread, and its admission slot
        #   is released right away;
        # * already RUNNING — the thread cannot be interrupted and keeps its
        #   admission until it finishes, but the cancelled wrapper never
        #   receives the eventual result or exception, so a late failure
        #   cannot fire asyncio's "Future exception was never retrieved"
        #   handler (elspeth-e4949acbe1: that traceback surfaced in the
        #   journal as a bogus request-id-middleware crash).
        #
        # The abandoned outcome is intentionally discarded: the caller has
        # already given up on it. A real infrastructure fault surfaces
        # through concurrent requests' own paths, not this echo. The executor
        # is the process-wide shared pool, so it is NOT shut down here —
        # teardown happens once via ``shutdown_async_workers()``.
        if future.done():
            # Completion can win the race with cancellation of the waiter.
            _retrieve_abandoned_exception(future)
        elif cancel_queued:
            future.cancel()
        else:
            # A refused request already owes this audit. Keep queued work
            # as well as running work; observe any late exception without
            # publishing a second response to a disconnected caller.
            future.add_done_callback(_retrieve_abandoned_exception)


async def _finish_joined_shared_future[T](
    future: ConcurrentFuture[T], cancellations: list[asyncio.CancelledError]
) -> RequiredSQLFinishOnce[T]:
    """Observe one actual Future without canceling or replacing its originals."""
    while not future.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as cancellation:
            _retain_cancellation(cancellations, cancellation)
    try:
        value = future.result()
    except BaseException as original:
        return RequiredSQLRaised(original, tuple(cancellations))
    return RequiredSQLReturned(value, tuple(cancellations))


def _assert_execution_generation(submission: ExecutionLeaseSQLSubmission) -> None:
    obligation = submission.obligation
    generation = obligation.generation
    _assert_recovery_registered()
    if (
        _APPLICATION_FINALIZER_OWNER is not obligation.registry.owner
        or _INSTANCE_DRAINING is not obligation.registry.recovery.instance_draining
    ):
        raise AuditIntegrityError("EXECUTE submission lost selected application ownership")
    with _EXECUTOR_LOCK:
        if _SHARED_EXECUTOR is not obligation.executor or _GENERATION_CUSTODIAN is not generation or _SHARED_SHUTDOWN_STARTED:
            raise RequiredGenerationUnavailable("EXECUTE submission lost its captured shared generation")
    if generation.state != "active" or _GENERATION_UNAVAILABLE.is_set():
        raise RequiredGenerationUnavailable("EXECUTE submission generation is unavailable")
    if _INSTANCE_DRAINING.is_set() and not submission.permits_process_drain:
        raise RequiredGenerationUnavailable("EXECUTE acquisition refused during application drain")


async def run_execution_lease_sql_finish_once(
    submission: ExecutionLeaseSQLSubmission,
) -> RequiredSQLFinishOnce[SessionOperationContext | None]:
    """Closed EXECUTE SQL dispatch with the same bounded physical pool custody."""
    if type(submission) is not ExecutionLeaseSQLSubmission:
        raise AuditIntegrityError("EXECUTE SQL bridge requires its issued invocation")
    invocation = submission.invoke
    if (
        type(invocation) is not MethodType
        or invocation.__self__ is not submission
        or invocation.__func__ is not ExecutionLeaseSQLSubmission.invoke
    ):
        raise AuditIntegrityError("EXECUTE SQL replaced its issued invocation")
    ExecutionLeaseSQLSubmission.assert_canonical_dispatch(submission)
    obligation = submission.obligation
    generation = obligation.generation
    cancellations: list[asyncio.CancelledError] = []
    loop = asyncio.get_running_loop()
    reservation = InvocationReservation(RequiredInvocationWitness(generation.identity), _release_admission)
    try:
        with generation.submission_lock:
            _assert_execution_generation(submission)
        admission_deadline = time.monotonic() + ADMISSION_WAIT_SECONDS
        while True:
            with generation.submission_lock:
                _assert_execution_generation(submission)
            if time.monotonic() >= admission_deadline:
                raise AsyncWorkerAdmissionTimeoutError("EXECUTE SQL admission expired without submission")
            if _try_admit():
                break
            await asyncio.sleep(min(_ADMISSION_POLL_SECONDS, max(0.0, admission_deadline - time.monotonic())))
    except BaseException as original:
        submission.observe_no_submission(_EXECUTION_SQL_BRIDGE_ISSUER_SEAL, original)
        return RequiredSQLRaised(original, tuple(cancellations))
    reservation.held = True
    future: ConcurrentFuture[SessionOperationContext | None] | None = None
    setup_failure: BaseException | None = None
    with generation.submission_lock:
        try:
            _assert_execution_generation(submission)
            with submission.submission_decision():
                generation.register(reservation)
                submission.bind_reservation(reservation)
                # Seal and dispatch decisions share this exact registry lock.
                # No callback can execute until after its release below.
                try:
                    future = obligation.executor.submit(reservation.witness.invoke, invocation)
                except BaseException as original:
                    setup_failure = original
                    reservation.submission_error = original
                    obligation.registry.record_failure(original)
                else:
                    reservation.future = future
                    submission.bind_future(future, reservation)
        except BaseException as original:
            if reservation._registering_generation is None:
                reservation.release_unused()
                submission.observe_no_submission(_EXECUTION_SQL_BRIDGE_ISSUER_SEAL, original)
                return RequiredSQLRaised(original, tuple(cancellations))
            setup_failure = original
            reservation.setup_error = original
            if reservation.future is None:
                reservation.submission_error = original
            obligation.registry.record_failure(original)
        if future is not None and setup_failure is None:
            try:

                def release_actual(done: ConcurrentFuture[SessionOperationContext | None]) -> None:
                    try:
                        reservation.release_future(done)
                        ExecutionLeaseSQLSubmission.observe_callback_return(submission, _EXECUTION_SQL_BRIDGE_ISSUER_SEAL, reservation)
                    except BaseException as original:
                        submission.projection_failure = original
                        obligation.registry.record_failure(original)
                        try:
                            loop.call_soon_threadsafe(generation.quarantine)
                        except BaseException as scheduling_original:
                            obligation.registry.record_failure(scheduling_original)

                future.add_done_callback(release_actual)
                reservation.witness.arm()
            except BaseException as original:
                setup_failure = original
                reservation.setup_error = original
                obligation.registry.record_failure(original)
    if setup_failure is not None:
        setup_originals: list[BaseException] = [setup_failure]
        obligation.registry.record_failure(setup_failure)
        # An arm implementation can publish ARMED then raise. Do not abort
        # that running SQL or replace its original setup fault with abort's
        # integrity refusal. WAITING alone authorizes the one abort attempt.
        if reservation.witness.snapshot().gate is InvocationGate.WAITING:
            try:
                reservation.witness.abort()
            except BaseException as abort_original:
                setup_originals.append(abort_original)
                obligation.registry.record_failure(abort_original)
        try:
            generation.quarantine()
        except BaseException as quarantine_original:
            setup_originals.append(quarantine_original)
            obligation.registry.record_failure(quarantine_original)
        if len(setup_originals) > 1:
            setup_failure = BaseExceptionGroup("EXECUTE setup retained original faults", setup_originals)
        # No-return custody needs the actual generation join: released is
        # published before the counter-release callback returns.
        while not reservation.released or (future is None and not generation.joined.is_set()):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as original:
                _retain_cancellation(cancellations, original)
        if future is not None:
            actual_outcome = await _finish_joined_shared_future(future, cancellations)
            trace = reservation.witness.snapshot()
            if trace.callable_started:
                while not submission.callback_return_observed and submission.projection_failure is None:
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as original:
                        _retain_cancellation(cancellations, original)
                submission.observe_future(future)
                failures = [setup_failure]
                if type(actual_outcome) is RequiredSQLRaised and actual_outcome.error is not setup_failure:
                    failures.append(actual_outcome.error)
                    obligation.registry.record_failure(actual_outcome.error)
                if submission.projection_failure is not None:
                    failures.append(submission.projection_failure)
                setup_sql_failure = (
                    failures[0] if len(failures) == 1 else BaseExceptionGroup("EXECUTE setup and actual SQL originals", failures)
                )
                return RequiredSQLRaised(setup_sql_failure, tuple(cancellations))
        while not (
            generation.joined.is_set() or (submission.callback_return_observed and reservation.witness.snapshot().valid_aborted_exit)
        ):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as original:
                _retain_cancellation(cancellations, original)
        submission.observe_aborted_submission(_EXECUTION_SQL_BRIDGE_ISSUER_SEAL, setup_failure)
        if submission.projection_failure is not None:
            return RequiredSQLRaised(
                BaseExceptionGroup("EXECUTE setup and callback originals", [setup_failure, submission.projection_failure]),
                tuple(cancellations),
            )
        return RequiredSQLRaised(setup_failure, tuple(cancellations))
    if future is None:
        failure = AuditIntegrityError("EXECUTE submission lacks its actual returned Future")
        obligation.registry.record_failure(failure)
        raise failure
    outcome = await _finish_joined_shared_future(future, cancellations)
    while not submission.callback_return_observed and submission.projection_failure is None:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            _retain_cancellation(cancellations, original)
    submission.observe_future(future)
    if submission.projection_failure is not None:
        projection_failure = submission.projection_failure
        if isinstance(outcome, RequiredSQLRaised) and outcome.error is not projection_failure:
            obligation.registry.record_failure(outcome.error)
            projection_failure = BaseExceptionGroup("EXECUTE custody and actual SQL originals", [projection_failure, outcome.error])
        return RequiredSQLRaised(projection_failure, tuple(cancellations))
    if isinstance(outcome, RequiredSQLRaised):
        return RequiredSQLRaised(outcome.error, tuple(cancellations))
    return RequiredSQLReturned(outcome.value, tuple(cancellations))


def _assert_current_watch_generation(
    reader: ComposerOperationWatchReader, executor: ThreadPoolExecutor, generation: RequiredExecutorGenerationCustodian
) -> None:
    if type(reader) is not ComposerOperationWatchReader or reader.executor is not executor or reader.generation is not generation:
        raise AuditIntegrityError("Operation observation generation ownership is foreign")
    with _EXECUTOR_LOCK:
        if _SHARED_EXECUTOR is not executor or _GENERATION_CUSTODIAN is not generation or _SHARED_SHUTDOWN_STARTED:
            raise RequiredGenerationUnavailable("Operation observation generation is no longer current")
    if generation.state != "active" or _submission_unavailable(None):
        raise RequiredGenerationUnavailable("Operation observation generation cannot submit")
    reader.check_cutoff()


async def _acquire_watch_admission(reader: ComposerOperationWatchReader) -> AsyncWorkerAdmissionTimeoutError | None:
    admission_deadline = time.monotonic() + ADMISSION_WAIT_SECONDS
    while True:
        executor = reader.executor
        generation = reader.generation
        if executor is None or generation is None:
            raise AuditIntegrityError("Operation observation admission lacks its actual generation")
        with generation.submission_lock:
            _assert_current_watch_generation(reader, executor, generation)
        if time.monotonic() >= admission_deadline:
            return AsyncWorkerAdmissionTimeoutError("Operation observation admission expired without submission")
        if _try_admit():
            return None
        await asyncio.sleep(min(_ADMISSION_POLL_SECONDS, max(0.0, admission_deadline - time.monotonic())))
