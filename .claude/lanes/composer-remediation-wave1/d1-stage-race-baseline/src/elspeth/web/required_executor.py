"""Physical custody for gated submissions to one shared executor generation."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.required_work import RequiredWorkTicket


class RequiredGenerationUnavailable(RuntimeError):
    """The exact shared generation cannot accept a fresh invocation."""


class RequiredInvocationIntegrityError(AuditIntegrityError):
    """An owned invocation witness has an impossible physical trace."""


class RequiredInvocationAborted(RuntimeError):
    """The unarmed wrapper exited without invoking its callable."""


class InvocationGate(Enum):
    WAITING = "waiting"
    ARMED = "armed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class RequiredGenerationDrainExpired:
    generation_identity: int
    captured_deadline: float
    outstanding_owned_ticket_identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    gate: InvocationGate
    entered: bool
    observed_aborted: bool
    callable_started: bool
    callable_finished: bool
    exited: bool

    @property
    def impossible(self) -> bool:
        return (
            (not self.entered and (self.observed_aborted or self.callable_started or self.callable_finished or self.exited))
            or (self.observed_aborted and self.callable_started)
            or (self.callable_finished and not self.callable_started)
        )

    @property
    def valid_aborted_exit(self) -> bool:
        return (
            self.gate is InvocationGate.ABORTED
            and self.entered
            and self.observed_aborted
            and self.exited
            and not (self.callable_started or self.callable_finished)
        )


class RequiredInvocationWitness:
    """Only the gated wrapper publishes the monotonic physical trace."""

    def __init__(self, generation_identity: int) -> None:
        self.identity = str(uuid4())
        self.generation_identity = generation_identity
        self._lock = threading.Lock()
        self._decision = threading.Event()
        self._gate = InvocationGate.WAITING
        self._entered = self._observed_aborted = self._started = self._finished = self._exited = False

    def snapshot(self) -> InvocationSnapshot:
        with self._lock:
            return InvocationSnapshot(self._gate, self._entered, self._observed_aborted, self._started, self._finished, self._exited)

    def arm(self) -> None:
        with self._lock:
            if self._gate is not InvocationGate.WAITING:
                raise RequiredInvocationIntegrityError("Invocation gate already decided")
            self._gate = InvocationGate.ARMED
            self._decision.set()

    def abort(self) -> None:
        with self._lock:
            if self._gate is InvocationGate.ARMED:
                raise RequiredInvocationIntegrityError("Cannot abort an armed invocation")
            self._gate = InvocationGate.ABORTED
            self._decision.set()

    def invoke[T](self, callable_: Callable[[], T]) -> T:
        with self._lock:
            if self._entered:
                raise RequiredInvocationIntegrityError("Duplicate wrapper entry")
            self._entered = True
        self._decision.wait()
        with self._lock:
            aborted = self._gate is InvocationGate.ABORTED
            if aborted:
                self._observed_aborted = True
            elif self._gate is InvocationGate.ARMED:
                self._started = True
            else:
                raise RequiredInvocationIntegrityError("Undecided gate after wake")
        try:
            if aborted:
                raise RequiredInvocationAborted("Invocation was never armed")
            try:
                return callable_()
            finally:
                with self._lock:
                    self._finished = True
        finally:
            with self._lock:
                self._exited = True


class InvocationReservation:
    """Admission is released exactly once, only after owned physical proof."""

    def __init__(self, witness: RequiredInvocationWitness, release: Callable[[], None], ticket: RequiredWorkTicket | None = None) -> None:
        self.witness = witness
        self.ticket = ticket
        self._release = release
        self._lock = threading.RLock()
        self.held = False
        self.released = False
        self.future: Future[Any] | None = None
        self.submission_error: BaseException | None = None
        self.setup_error: BaseException | None = None
        self.setup_outcome_error: BaseException | None = None
        self.integrity_error: RequiredInvocationIntegrityError | None = None
        self.semantic_observation_error: BaseException | None = None
        self.completion_observer: Callable[[InvocationReservation], None] | None = None
        self._registering_generation: RequiredExecutorGenerationCustodian | None = None
        self._completion_recorded = False

    def release_unused(self) -> None:
        if self.future is not None or self.submission_error is not None:
            raise RequiredInvocationIntegrityError("Used reservation cannot release as unused")
        self._release_once()

    def release_future(self, future: Future[Any]) -> None:
        if future is not self.future or not future.done():
            raise RequiredInvocationIntegrityError("Future completion proof mismatch")
        try:
            if self.ticket is not None and not self.ticket.complete:
                if self.setup_error is not None and self.witness.snapshot().valid_aborted_exit:
                    self.ticket.observe_aborted_future_outcome(self.setup_error)
                else:
                    self.ticket.observe_actual_outcome()
        except BaseException as observation_failure:
            self.semantic_observation_error = observation_failure
            # Physical actual-Future completion remains true; logical ticket
            # observation is separately retained and cannot erase that proof.
        self._release_once()
        if self.completion_observer is not None:
            self.completion_observer(self)

    def release_aborted(self) -> None:
        if self.future is not None or self.submission_error is None or not self.witness.snapshot().valid_aborted_exit:
            raise RequiredInvocationIntegrityError("Aborted invocation proof incomplete")
        with self._lock:
            if self.released:
                return
            if self.ticket is not None:
                self.ticket.observe_aborted_invocation_exit(self.submission_error)
            self._release_once()

    def release_joined(self) -> None:
        if self.witness.snapshot().gate is not InvocationGate.ABORTED or self.future is not None:
            raise RequiredInvocationIntegrityError("Generation join does not prove this invocation aborted")
        with self._lock:
            if self.released:
                return
            if self.ticket is not None and self.submission_error is not None:
                self.ticket.complete_generation_joined(self.submission_error)
            self._release_once()

    def _release_once(self) -> None:
        with self._lock:
            if not self.held or self.released:
                return
            self.released = True
        self._release()


class RequiredExecutorGenerationCustodian:
    """No replacement can overlap the exact quarantined executor's workers."""

    def __init__(
        self,
        executor: ThreadPoolExecutor,
        *,
        generation_identity: int,
        drain_seconds: float,
        generation_unavailable: threading.Event,
        instance_draining: threading.Event,
        recovery_callback: Callable[[RequiredGenerationDrainExpired], None],
        replacement_factory: Callable[[], ThreadPoolExecutor],
        install_replacement: Callable[[ThreadPoolExecutor], None],
    ) -> None:
        self.executor = executor
        self.identity = generation_identity
        self.submission_lock = threading.RLock()
        self.generation_unavailable = generation_unavailable
        self.instance_draining = instance_draining
        self._callback = recovery_callback
        self._budget = min(drain_seconds, 30.0)
        self._replacement_factory = replacement_factory
        self._install_replacement = install_replacement
        self.state = "active"
        self.reservations: dict[str, InvocationReservation] = {}
        self.deadline: float | None = None
        self.drain_thread: threading.Thread | None = None
        self.observer: asyncio.Task[None] | None = None
        self.joined = threading.Event()
        self.recovery_finished = threading.Event()
        self.drain_error: BaseException | None = None
        self.observer_error: BaseException | None = None
        self.escalated = False

    def register(self, reservation: InvocationReservation) -> None:
        if not isinstance(reservation, InvocationReservation):
            raise RequiredInvocationIntegrityError("Generation registration requires an owned reservation")
        with self.submission_lock:
            if reservation._registering_generation is not None or reservation.witness.identity in self.reservations:
                raise RequiredInvocationIntegrityError("Reservation registration ownership is duplicate")
            reservation._registering_generation = self
            self.reservations[reservation.witness.identity] = reservation
            reservation.completion_observer = self.record_completed

    def record_completed(self, reservation: InvocationReservation) -> None:
        if not isinstance(reservation, InvocationReservation):
            raise RequiredInvocationIntegrityError("Completion requires an owned reservation")
        with self.submission_lock:
            if reservation._registering_generation is not self:
                raise RequiredInvocationIntegrityError("Reservation completion has foreign generation ownership")
            if self.state == "active" and reservation.released:
                if reservation._completion_recorded:
                    return
                if reservation.witness.identity not in self.reservations:
                    raise RequiredInvocationIntegrityError("Reservation completion lacks its registered identity")
                if self.reservations[reservation.witness.identity] is not reservation:
                    raise RequiredInvocationIntegrityError("Reservation completion registry identity mismatch")
                del self.reservations[reservation.witness.identity]
                reservation._completion_recorded = True

    def quarantine(self) -> None:
        if self.state != "active":
            return
        self.state = "quarantined"
        self.generation_unavailable.set()
        self.deadline = time.monotonic() + self._budget
        self.drain_thread = threading.Thread(target=self._drain, name="required-generation-drain", daemon=True)
        self.drain_thread.start()
        self.observer = asyncio.create_task(self._observe(), name="required-generation-observer")
        self.observer.add_done_callback(self._observer_finished)

    def _drain(self) -> None:
        try:
            self.executor.shutdown(wait=True, cancel_futures=False)
            with self.submission_lock:
                for reservation in self.reservations.values():
                    if reservation.future is None and reservation.submission_error is not None:
                        reservation.release_joined()
                    elif reservation.future is not None:
                        if not reservation.future.done():
                            raise RequiredInvocationIntegrityError("Joined executor retained incomplete Future")
                        reservation.release_future(reservation.future)
                self.joined.set()
                if not self.escalated and not self.instance_draining.is_set():
                    replacement = self._replacement_factory()
                    self._install_replacement(replacement)
                    self.state = "closed"
                    self.generation_unavailable.clear()
                self.recovery_finished.set()
        except BaseException as exc:
            self.drain_error = exc

    def _observer_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self.observer_error = asyncio.CancelledError()
        else:
            self.observer_error = task.exception()

    async def _observe(self) -> None:
        try:
            while not self.recovery_finished.is_set():
                with self.submission_lock:
                    for reservation in self.reservations.values():
                        if reservation.witness.snapshot().impossible and reservation.integrity_error is None:
                            reservation.integrity_error = RequiredInvocationIntegrityError("Impossible invocation witness")
                            if reservation.ticket is not None:
                                reservation.ticket.observe_custody_failure(reservation.integrity_error)
                        if (
                            reservation.future is None
                            and reservation.submission_error is not None
                            and reservation.witness.snapshot().valid_aborted_exit
                        ):
                            reservation.release_aborted()
                    if self.deadline is None:
                        raise RequiredInvocationIntegrityError("Quarantine observer lacks deadline")
                    if time.monotonic() >= self.deadline:
                        self.escalated = True
                        self.instance_draining.set()
                        self._callback(
                            RequiredGenerationDrainExpired(
                                self.identity,
                                self.deadline,
                                tuple(identity for identity, reservation in self.reservations.items() if not reservation.released),
                            )
                        )
                        return
                await asyncio.sleep(0.01)
        except BaseException as exc:
            self.observer_error = exc
            raise
