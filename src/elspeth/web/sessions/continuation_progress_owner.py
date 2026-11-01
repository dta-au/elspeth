"""Segment-local advisory progress custody; never portable execution DATA.

This owner uses the existing registry and executor admission. In particular,
ordinary cleanup cannot bypass drain or a quarantined executor generation.
The segment must join ``close`` before releasing its physical lease.
"""

from __future__ import annotations

import asyncio
import math

from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError

from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.composer.progress import ComposerProgressRegistry, ComposerRequestLease, model_call_progress_event
from elspeth.web.coordination.composer_progress_authority import (
    ComposerProgressIdentityInactive,
    ComposerProgressSessionUnavailable,
    ComposerRequestLeaseLost,
    DatabaseComposerProgressRegistry,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning

_ADVISORY_FAILURES = (
    ComposerRequestLeaseLost,
    ComposerProgressIdentityInactive,
    ComposerProgressSessionUnavailable,
    OperationalError,
    SQLAlchemyPoolTimeoutError,
    AsyncWorkerAdmissionTimeoutError,
    RequiredGenerationUnavailable,
)


def _combine(original: BaseException, *others: BaseException) -> BaseException:
    from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

    return combine_failure_objects(original, *others)


async def _join[T](task: asyncio.Task[T]) -> T:
    """Retrieve the actual child outcome, then propagate caller cancellations."""
    cancellations: list[asyncio.CancelledError] = []
    caller = asyncio.current_task()
    while not task.done():
        before = caller.cancelling() if caller is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if not task.cancelled() or (caller is not None and caller.cancelling() > before):
                cancellations.append(error)
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as original:
        if cancellations:
            combined = _combine(original, *cancellations)
            raise combined from combined.__cause__
        raise
    if cancellations:
        combined = _combine(cancellations[0], *cancellations[1:])
        raise combined from combined.__cause__
    return result


class ContinuationProgressOwner:
    """One fresh progress lease/callback for one actual running segment.

    Named advisory failures disable this channel without cancelling business
    work. Coding/integrity failures remain actual exceptions. Failed cleanup is
    visible and never represented as successful exact-token unregister.
    """

    def __init__(
        self,
        *,
        registry: ComposerProgressRegistry | DatabaseComposerProgressRegistry,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        record: ComposerOperationRecord,
        renew_interval_seconds: float,
    ) -> None:
        if type(registry) not in (ComposerProgressRegistry, DatabaseComposerProgressRegistry):
            raise TypeError("segment progress requires its actual registry")
        if type(running) is not ComposerOperationRunning or type(lease) is not SessionOperationLease:
            raise TypeError("segment progress requires actual running and physical lease owners")
        if type(record) is not ComposerOperationRecord or lease.context is not running.session_operation_context:
            raise AuditIntegrityError("segment progress changed its actual physical owner")
        fence = running.session_operation_context.fence
        if (
            record.session_id,
            record.operation_id,
            record.attempt,
            record.current_segment_sequence,
            record.ownership_generation,
            record.session_operation_id,
            record.session_operation_epoch,
            record.status,
        ) != (
            running.claim.session_id,
            running.claim.operation_id,
            running.claim.attempt,
            running.claim.segment_sequence,
            running.claim.ownership_generation,
            fence.operation_id,
            fence.operation_epoch,
            "running",
        ):
            raise AuditIntegrityError("segment progress changed its exact original record/fence")
        if type(renew_interval_seconds) not in (int, float) or not math.isfinite(renew_interval_seconds) or renew_interval_seconds <= 0:
            raise ValueError("segment progress renewal interval must be finite and positive")
        self.registry, self.running, self.lease, self.record = registry, running, lease, record
        self._renew_interval = renew_interval_seconds
        self._retired = asyncio.Event()
        self._request_lease: ComposerRequestLease | None = None
        self._publisher: ComposerProgressSink | None = None
        self._starting: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._explicit_close_started = False
        self._inflight: set[asyncio.Task[None]] = set()
        self._entry_tail: asyncio.Task[None] | None = None
        self._entry_fallback: asyncio.Task[None] | None = None
        self._entry_defects: list[BaseException] = []
        self._defects: list[BaseException] = []
        self.advisory_failures: list[BaseException] = []
        self.cleanup_failure: BaseException | None = None
        self.unregistered = False
        self.sink: ComposerProgressSink = self.publish

    @property
    def request_lease(self) -> ComposerRequestLease | None:
        return self._request_lease

    def _disable(self, error: BaseException) -> None:
        if all(error is not previous for previous in self.advisory_failures):
            self.advisory_failures.append(error)
        self._retired.set()

    async def start(self) -> None:
        if self._starting is None:
            self._starting = asyncio.create_task(self._start(), name="continuation-progress-start")
        try:
            await _join(self._starting)
        except BaseException as original:
            try:
                await self.close()
            except BaseException as cleanup:
                combined = _combine(original, cleanup)
                if combined is original:
                    raise original from original.__cause__
                raise combined from combined.__cause__
            raise

    async def _start(self) -> None:
        if self._retired.is_set():
            return
        try:
            self._request_lease = await self.registry.start_request(str(self.record.session_id), self.record.actor_user_id)
            if self._retired.is_set():
                return
            fence = self.running.session_operation_context.fence
            self._publisher = await self.registry.claim_request(
                session_id=str(self.record.session_id),
                request_id=str(self.record.reserved_user_message_id),
                user_id=self.record.actor_user_id,
                lease=self._request_lease,
                operation_id=self.record.operation_id,
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
            )
        except _ADVISORY_FAILURES as error:
            self._disable(error)
            return
        if not self._retired.is_set():
            self._heartbeat = asyncio.create_task(self._renew(), name="continuation-progress-heartbeat")

    async def _renew(self) -> None:
        try:
            while not self._retired.is_set():
                try:
                    await asyncio.wait_for(self._retired.wait(), timeout=self._renew_interval)
                    return
                except TimeoutError:
                    pass
                lease = self._request_lease
                if lease is None:
                    raise AuditIntegrityError("progress heartbeat lost its actual request lease")
                try:
                    await self.registry.renew_request(lease)
                except _ADVISORY_FAILURES as error:
                    self._disable(error)
        except BaseException as error:
            self._defects.append(error)
            self._retired.set()
            raise

    def notify_sdk_entry(self) -> None:
        """Queue one owned, safe event after actual PRIMARY SDK custody entered.

        This synchronous cut cannot await registry I/O or raise into a paid
        provider call. The queued writer remains owned through ``close``.
        """
        if self._retired.is_set():
            if self._explicit_close_started:
                self._entry_defects.append(AuditIntegrityError("SDK entry after explicit progress retirement"))
            return
        try:
            publisher = self._publisher
            if publisher is None or self._entry_fallback is not None:
                raise AuditIntegrityError("SDK-entry progress lost its started publication owner")
            event = model_call_progress_event("")
            previous = self._entry_tail
            writing = self._write_sdk_entry(publisher, event, previous)
            try:
                task = asyncio.create_task(writing, name="continuation-progress-sdk-entry")
            except BaseException:
                writing.close()
                raise
            # Retain the task even if set registration itself fails. No event
            # loop suspension occurs between scheduling and these assignments.
            self._entry_fallback = task
            self._inflight.add(task)
            self._entry_tail = task
            self._entry_fallback = None
        except BaseException as error:
            self._entry_defects.append(error)

    async def _write_sdk_entry(
        self,
        publisher: ComposerProgressSink,
        event: ComposerProgressEvent,
        previous: asyncio.Task[None] | None,
    ) -> None:
        # An eager task factory must not run registry I/O in the SDK-entry
        # callback before the provider can be invoked.
        await asyncio.sleep(0)
        try:
            if previous is not None:
                await _join(previous)
            await publisher(event)
        except _ADVISORY_FAILURES as error:
            self._disable(error)
        except BaseException as error:
            self._entry_defects.append(error)

    async def publish(self, event: ComposerProgressEvent) -> None:
        if self._defects:
            raise self._defects[0]
        if self._retired.is_set():
            return
        if type(event) is not ComposerProgressEvent or self._publisher is None:
            raise AuditIntegrityError("segment progress requires its started callback and owned event")
        entry = self._entry_fallback if self._entry_fallback is not None else self._entry_tail
        if entry is not None:
            await _join(entry)
            if self._retired.is_set():
                return
            if self._defects:
                raise self._defects[0]

        async def write() -> None:
            publisher = self._publisher
            if publisher is None:
                raise AuditIntegrityError("segment progress lost its claimed callback")
            try:
                await publisher(event)
            except _ADVISORY_FAILURES as error:
                self._disable(error)

        task = asyncio.create_task(write(), name="continuation-progress-publish")
        self._inflight.add(task)
        try:
            await _join(task)
        except BaseException:
            if task.done() and not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    self._defects.append(failure)
            raise
        finally:
            self._inflight.discard(task)

    async def close(self) -> None:
        self._explicit_close_started = True
        self._retired.set()
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(), name="continuation-progress-close")
        await _join(self._closing)
        # A callback can arrive after the original close task has finished.
        # Its retained defect must remain observable on a repeated close.
        if self._entry_defects:
            combined = _combine(self._entry_defects[0], *self._entry_defects[1:])
            raise combined from combined.__cause__

    async def _close(self) -> None:
        errors: list[BaseException] = []
        for task in (self._starting, self._heartbeat, *tuple(self._inflight), self._entry_fallback):
            if task is not None:
                try:
                    await _join(task)
                except BaseException as error:
                    errors.append(error)
        if self._request_lease is not None:
            try:
                # Deliberately ordinary admission: this cannot defeat drain or
                # executor quarantine. A narrow owned drain cleanup is separate.
                await self.registry.finish_request(self._request_lease)
            except BaseException as error:
                self.cleanup_failure = error
                errors.append(error)
            else:
                self.unregistered = True
        errors.extend(self._defects)
        errors.extend(self._entry_defects)
        if errors:
            combined = _combine(errors[0], *errors[1:])
            raise combined from combined.__cause__
