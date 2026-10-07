"""Private ownership of one exact durable-operation observation stream."""

from __future__ import annotations

import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.required_executor import InvocationReservation, RequiredExecutorGenerationCustodian, RequiredGenerationUnavailable

if TYPE_CHECKING:
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning
    from elspeth.web.sessions.composer_turn import ComposerBudgetAnchor


_WATCH_CREATION_SEAL = object()
_WATCH_REFUSAL_ISSUER_SEAL = object()


@dataclass(frozen=True, slots=True)
class _NoSubmissionAdmissionRefusal:
    owner: ComposerOperationWatchReader
    executor: ThreadPoolExecutor
    generation: RequiredExecutorGenerationCustodian
    reservation: InvocationReservation
    original: BaseException


class ComposerOperationWatchReader:
    def __init__(
        self,
        seal: object,
        worker: ComposerAsyncWorker,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        anchor: ComposerBudgetAnchor,
    ) -> None:
        if seal is not _WATCH_CREATION_SEAL:
            raise AuditIntegrityError("Operation observation owner is worker-issued")
        self.worker = worker
        self.running = running
        self.lease = lease
        self.anchor = anchor
        self.deadline = anchor.monotonic_at_running + anchor.remaining_at_running_seconds
        if (
            not math.isfinite(self.deadline)
            or not math.isfinite(anchor.remaining_at_running_seconds)
            or anchor.remaining_at_running_seconds <= 0
        ):
            raise AuditIntegrityError("Operation observation has invalid original budget")
        self.executor: ThreadPoolExecutor | None = None
        self.generation: RequiredExecutorGenerationCustodian | None = None
        self._issued: _NoSubmissionAdmissionRefusal | None = None
        self._registered_issuance: _NoSubmissionAdmissionRefusal | None = None
        self._refusals: list[BaseException] = []
        self._internal_cleanup_marker: object | None = None
        self._cleanup_cancellations: list[asyncio.CancelledError] = []
        self._physical_failure_originals: list[BaseException] = []
        self._stop_wake_observed = False
        self._invocation = worker._authority.get_with_database_now

    @classmethod
    def for_operation(
        cls,
        worker: ComposerAsyncWorker,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        anchor: ComposerBudgetAnchor,
    ) -> ComposerOperationWatchReader:
        from elspeth.web.coordination.lifecycle import SessionOperationLease
        from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
        from elspeth.web.sessions.composer_operations import ComposerOperationRunning
        from elspeth.web.sessions.composer_turn import ComposerBudgetAnchor

        if (
            type(worker) is not ComposerAsyncWorker
            or type(running) is not ComposerOperationRunning
            or type(lease) is not SessionOperationLease
            or type(anchor) is not ComposerBudgetAnchor
            or lease.context != running.session_operation_context
        ):
            raise AuditIntegrityError("Operation observation lacks exact owned running authority")
        return cls(_WATCH_CREATION_SEAL, worker, running, lease, anchor)

    def invoke(self) -> tuple[ComposerOperationRecord | None, datetime]:
        return self._invocation(session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id)

    def pin_generation(self, executor: ThreadPoolExecutor, generation: RequiredExecutorGenerationCustodian) -> None:
        if self.executor is None and self.generation is None:
            self.executor = executor
            self.generation = generation
        elif self.executor is not executor or self.generation is not generation:
            raise RequiredGenerationUnavailable("Operation observation generation changed")

    def check_cutoff(self) -> None:
        from elspeth.web.sessions.composer_operations import COMPOSER_LEASE_LOST, COMPOSER_SHUTDOWN, ComposerTurnDeadlineExpired

        if self.lease.renewal_error is not None:
            raise self.lease.renewal_error
        if self.lease.closed:
            raise asyncio.CancelledError(COMPOSER_LEASE_LOST)
        if self.worker._stopping or self.worker._instance_draining.is_set():
            raise asyncio.CancelledError(COMPOSER_SHUTDOWN)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ComposerTurnDeadlineExpired(
                session_id=self.running.claim.session_id,
                operation_id=self.running.claim.operation_id,
                remaining_seconds=max(0.0, remaining),
                budget_seconds_at_running=self.anchor.remaining_at_running_seconds,
            )

    def issue_refusal(
        self,
        issuer_seal: object,
        original: BaseException,
        executor: ThreadPoolExecutor,
        generation: RequiredExecutorGenerationCustodian,
        reservation: InvocationReservation,
    ) -> None:
        from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError

        snapshot = reservation.witness.snapshot()
        if (
            issuer_seal is not _WATCH_REFUSAL_ISSUER_SEAL
            or type(original) is not AsyncWorkerAdmissionTimeoutError
            or self._issued is not None
            or self._registered_issuance is not None
            or self.executor is not executor
            or self.generation is not generation
            or reservation.held
            or reservation.future is not None
            or reservation._registering_generation is not None
            or reservation.submission_error is not None
            or snapshot.entered
            or snapshot.callable_started
        ):
            raise AuditIntegrityError("Operation observation refusal lacks issued no-submission custody")
        self._issued = _NoSubmissionAdmissionRefusal(self, executor, generation, reservation, original)
        self._registered_issuance = self._issued

    def consume_refusal(self, original: BaseException) -> None:
        from elspeth.web.async_workers import _assert_current_watch_generation

        receipt = self._issued
        if receipt is None or receipt is not self._registered_issuance or receipt.owner is not self or receipt.original is not original:
            raise AuditIntegrityError("Operation observation refusal is foreign or already consumed")
        with receipt.generation.submission_lock:
            _assert_current_watch_generation(self, receipt.executor, receipt.generation)
            snapshot = receipt.reservation.witness.snapshot()
            if (
                receipt.reservation.held
                or receipt.reservation.future is not None
                or receipt.reservation._registering_generation is not None
                or snapshot.entered
                or snapshot.callable_started
            ):
                raise AuditIntegrityError("Operation observation refusal no longer proves no submission")
            self._issued = None
            self._registered_issuance = None
            self.retain_refusal_original(original)
            self.check_cutoff()

    def retain_refusal_original(self, original: BaseException) -> None:
        if all(original is not earlier for earlier in self._refusals):
            self._refusals.append(original)

    def request_internal_cleanup(self, marker: object) -> None:
        if self._internal_cleanup_marker is not None and self._internal_cleanup_marker is not marker:
            raise AuditIntegrityError("Operation observation cleanup identity changed")
        self._internal_cleanup_marker = marker

    async def wait_retry_cadence(self) -> None:
        event = self.worker._local_cancels.get((self.running.claim.session_id, self.running.claim.operation_id))
        if event is not None and event.is_set() and not self._stop_wake_observed:
            self._stop_wake_observed = True
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.25)

    def _separate_delivered_cancellations(self, cancellations: list[asyncio.CancelledError]) -> list[asyncio.CancelledError]:
        """Separate only loop-delivered private close from business originals.

        An actual SQL outcome is never passed here. Its exception may itself
        be CancelledError and remains a business outcome regardless of args.
        """
        business: list[asyncio.CancelledError] = []
        for original in cancellations:
            if self._is_private_loop_delivery(original):
                if all(original is not earlier for earlier in self._cleanup_cancellations):
                    self._cleanup_cancellations.append(original)
            else:
                business.append(original)
        return business

    def retain_physical_failure_original(self, original: BaseException) -> None:
        if all(original is not earlier for earlier in self._physical_failure_originals):
            self._physical_failure_originals.append(original)

    def _is_private_loop_delivery(self, original: asyncio.CancelledError) -> bool:
        return (
            self._internal_cleanup_marker is not None
            and len(original.args) == 1
            and original.args[0] is self._internal_cleanup_marker
            and all(original is not physical for physical in self._physical_failure_originals)
        )

    def owns_internal_cleanup_observation(self, original: asyncio.CancelledError) -> bool:
        return self._is_private_loop_delivery(original) and any(original is retained for retained in self._cleanup_cancellations)

    def retain_join_loop_delivery(self, original: asyncio.CancelledError) -> None:
        """Retain exact outer-watch delivery while joining its loss child.

        Child result exceptions must never enter this provenance boundary.
        """
        self._separate_delivered_cancellations([original])

    async def wait_watch_cadence(self) -> None:
        """Record loop-delivered close while outside a physical SQL read."""
        try:
            await asyncio.sleep(0.25)
        except asyncio.CancelledError as original:
            self._separate_delivered_cancellations([original])
            raise

    async def read(self) -> tuple[ComposerOperationRecord | None, datetime]:
        from elspeth.web.async_workers import (
            AsyncWorkerAdmissionTimeoutError,
            _finish_joined_shared_future,
            _raise_lifecycle_originals,
            _retain_cancellation,
            _submit_shared,
        )
        from elspeth.web.required_sql_outcomes import RequiredSQLRaised

        cancellations: list[asyncio.CancelledError] = []
        try:
            while True:
                try:
                    future = await _submit_shared(self.invoke, watch_reader=self, deferred_cancellations=cancellations)
                except AsyncWorkerAdmissionTimeoutError as refusal:
                    if self._issued is None or self._issued.original is not refusal:
                        raise
                    self.consume_refusal(refusal)
                    await self.wait_retry_cadence()
                    continue
                outcome = await _finish_joined_shared_future(future, cancellations)
                if isinstance(outcome, RequiredSQLRaised):
                    self.retain_physical_failure_original(outcome.error)
                    raise outcome.error
                value = outcome.value
                break
        except BaseException as original:
            business = self._separate_delivered_cancellations(cancellations)
            if isinstance(original, asyncio.CancelledError) and self._is_private_loop_delivery(original):
                self._separate_delivered_cancellations([original])
                _raise_lifecycle_originals(business, list(self._refusals) if business else [])
                raise
            if isinstance(original, asyncio.CancelledError):
                _retain_cancellation(business, original)
            _raise_lifecycle_originals(business, [*self._refusals, original])
            raise
        # Project originals once. Catching our own group above would duplicate
        # the delivered cancellation leaves by nesting that group with them.
        business = self._separate_delivered_cancellations(cancellations)
        _raise_lifecycle_originals(business, list(self._refusals) if business else [])
        if self._cleanup_cancellations:
            raise self._cleanup_cancellations[-1]
        return value
