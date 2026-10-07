"""Closed single-use admission capabilities for application teardown only."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elspeth.web.required_executor import InvocationReservation

from elspeth.contracts.errors import AuditIntegrityError


class ApplicationFinalizerKind(Enum):
    MEMBERSHIP_DRAIN = "membership_drain"
    MEMBERSHIP_STOP = "membership_stop"
    EXECUTION_EXECUTOR_JOIN = "execution_executor_join"


_CREATION_SEAL = object()
_FINALIZER_PHYSICAL_ISSUER_SEAL = object()


class ApplicationFinalizerCapability:
    def __init__(
        self, seal: object, owner: ApplicationFinalizerOwner, kind: ApplicationFinalizerKind, invocation: Callable[[], object]
    ) -> None:
        if seal is not _CREATION_SEAL:
            raise TypeError("Finalizer capability is lifecycle-owned")
        self.owner = owner
        self.kind = kind
        self._invocation = invocation
        self.claimed = False
        self._invocation_lock = threading.Lock()
        self._invoked = False
        self._physical_reservation: InvocationReservation | None = None
        self._physical_future: Future[Any] | None = None
        self._physical_failures: list[BaseException] = []
        self._physical_callback_return_observed = False
        self._physical_admission_release_return_observed = False
        self._physical_callback_failure_exit_observed = False

    def _bind_physical_reservation(self, seal: object, reservation: InvocationReservation) -> None:
        from elspeth.web.required_executor import InvocationReservation

        with self._invocation_lock:
            if (
                seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL
                or type(reservation) is not InvocationReservation
                or not self.claimed
                or self._physical_reservation is not None
                or reservation._registering_generation is None
            ):
                raise AuditIntegrityError("Finalizer physical reservation is not bridge-issued")
            self._physical_reservation = reservation

    def _bind_physical_future[T](self, seal: object, future: Future[T]) -> None:
        with self._invocation_lock:
            reservation = self._physical_reservation
            if (
                seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL
                or reservation is None
                or reservation.future is not future
                or self._physical_future is not None
            ):
                raise AuditIntegrityError("Finalizer physical Future lost its bridge reservation")
            self._physical_future = future

    def _observe_physical_callback_return(self, seal: object, reservation: InvocationReservation) -> None:
        with self._invocation_lock:
            if (
                seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL
                or reservation is not self._physical_reservation
                or self._physical_future is not reservation.future
                or not reservation.released
                or reservation.semantic_observation_error is not None
            ):
                raise AuditIntegrityError("Finalizer callback lacks exact successful physical release return")
            self._physical_callback_return_observed = True

    def _observe_physical_admission_release_return(self, seal: object, reservation: InvocationReservation) -> None:
        """Called only after this reservation's actual counter release returns."""
        if not self.owner.owns_claimed(self):
            raise AuditIntegrityError("Finalizer admission receipt lost its registered capability")
        with self._invocation_lock:
            if seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL or reservation is not self._physical_reservation or not reservation.released:
                raise AuditIntegrityError("Finalizer admission receipt lost its exact reservation")
            self._physical_admission_release_return_observed = True

    def _observe_physical_callback_failure_exit(self, seal: object, reservation: InvocationReservation) -> None:
        """A failed callback returned; this does not establish physical custody."""
        if not self.owner.owns_claimed(self):
            raise AuditIntegrityError("Finalizer failed callback lost its registered capability")
        with self._invocation_lock:
            if (
                seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL
                or reservation is not self._physical_reservation
                or self._physical_future is not reservation.future
                or not self._physical_failures
            ):
                raise AuditIntegrityError("Finalizer failed callback lacks its retained original")
            self._physical_callback_failure_exit_observed = True

    @property
    def physical_failure_completion_known(self) -> bool:
        """Known failed closure after actual release and exact generation join."""
        if not self.owner.owns_claimed(self):
            return False
        with self._invocation_lock:
            reservation = self._physical_reservation
            future = self._physical_future
            if (
                reservation is None
                or future is None
                or not future.done()
                or reservation.future is not future
                or not reservation.released
                or not self._physical_admission_release_return_observed
                or not self._physical_callback_failure_exit_observed
                or not self._physical_failures
                or not self._invoked
            ):
                return False
            generation = reservation._registering_generation
            if generation is None or not generation.joined.is_set():
                return False
            trace = reservation.witness.snapshot()
            return not trace.impossible and trace.callable_finished and trace.exited

    @property
    def physical_submission_failure_completion_known(self) -> bool:
        """A failed submit/setup owns no unjoined invocation or admission."""
        if not self.owner.owns_claimed(self):
            return False
        with self._invocation_lock:
            reservation = self._physical_reservation
            if (
                reservation is None
                or not reservation.released
                or not self._physical_admission_release_return_observed
                or not self._physical_failures
                or (reservation.setup_error is None and reservation.submission_error is None)
            ):
                return False
            generation = reservation._registering_generation
            if generation is None or not generation.joined.is_set():
                return False
            trace = reservation.witness.snapshot()
            if trace.impossible:
                return False
            future = reservation.future
            if future is None:
                from elspeth.web.required_executor import InvocationGate

                return trace.gate is InvocationGate.ABORTED and not trace.callable_started
            if future is not self._physical_future or not future.done():
                return False
            return trace.valid_aborted_exit or (self._invoked and trace.callable_finished and trace.exited)

    def _retain_physical_failure(self, seal: object, original: BaseException) -> None:
        with self._invocation_lock:
            reservation = self._physical_reservation
            future = self._physical_future
            if seal is not _FINALIZER_PHYSICAL_ISSUER_SEAL or reservation is None:
                raise AuditIntegrityError("Finalizer failure is not bridge-issued")
            known_setup = (
                original is reservation.setup_error
                or original is reservation.submission_error
                or original is reservation.semantic_observation_error
            )
            known_actual = future is not None and future.done() and not future.cancelled() and future.exception() is original
            if not known_setup and not known_actual:
                raise AuditIntegrityError("Finalizer failure changed its physical original")
            if all(original is not earlier for earlier in self._physical_failures):
                self._physical_failures.append(original)

    @property
    def physical_failure_originals(self) -> tuple[BaseException, ...]:
        with self._invocation_lock:
            return tuple(self._physical_failures)

    @property
    def physical_completion_known(self) -> bool:
        if not self.owner.owns_claimed(self):
            return False
        with self._invocation_lock:
            reservation = self._physical_reservation
            future = self._physical_future
            if (
                reservation is None
                or future is None
                or not future.done()
                or not reservation.released
                or not self._invoked
                or not self._physical_callback_return_observed
                or not self._physical_admission_release_return_observed
                or self._physical_failures
            ):
                return False
            trace = reservation.witness.snapshot()
            return not trace.impossible and trace.callable_finished and trace.exited and reservation.future is future

    @property
    def physical_completion_succeeded(self) -> bool:
        if not self.owner.owns_claimed(self):
            return False
        with self._invocation_lock:
            reservation = self._physical_reservation
            future = self._physical_future
            if (
                reservation is None
                or future is None
                or not future.done()
                or not reservation.released
                or not self._invoked
                or not self._physical_callback_return_observed
                or not self._physical_admission_release_return_observed
                or self._physical_failures
            ):
                return False
            trace = reservation.witness.snapshot()
            if trace.impossible or not trace.callable_finished or not trace.exited or reservation.future is not future:
                return False
            try:
                future.result()
            except BaseException:
                return False
            return True

    def invoke(self) -> object:
        with self._invocation_lock:
            if not self.claimed:
                raise AuditIntegrityError("Finalizer invocation was not claimed")
            if self._invoked:
                raise AuditIntegrityError("Finalizer invocation was reused")
            self._invoked = True
        return self._invocation()


class ApplicationFinalizerOwner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._capabilities: dict[ApplicationFinalizerKind, ApplicationFinalizerCapability] = {}
        self._sealed = False

    def register(self, kind: ApplicationFinalizerKind, invocation: Callable[[], object]) -> ApplicationFinalizerCapability:
        if type(kind) is not ApplicationFinalizerKind:
            raise TypeError("Finalizer kind must be owned")
        with self._lock:
            if self._sealed or kind in self._capabilities:
                raise AuditIntegrityError("Finalizer registration closed or duplicate")
            capability = ApplicationFinalizerCapability(_CREATION_SEAL, self, kind, invocation)
            self._capabilities[kind] = capability
            return capability

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def claim(self, capability: ApplicationFinalizerCapability) -> None:
        with self._lock:
            if not isinstance(capability, ApplicationFinalizerCapability) or capability.owner is not self:
                raise AuditIntegrityError("Foreign application finalizer owner")
            if not self._sealed or self._capabilities.get(capability.kind) is not capability or capability.claimed:
                raise AuditIntegrityError("Application finalizer unavailable or reused")
            capability.claimed = True

    def owns_claimed(self, capability: ApplicationFinalizerCapability) -> bool:
        with self._lock:
            return (
                capability.owner is self and self._sealed and self._capabilities.get(capability.kind) is capability and capability.claimed
            )

    def owns_registered(self, capability: ApplicationFinalizerCapability) -> bool:
        """Identity only: this does not claim or grant invocation authority."""
        with self._lock:
            return (
                type(capability) is ApplicationFinalizerCapability
                and capability.owner is self
                and type(capability.kind) is ApplicationFinalizerKind
                and self._capabilities.get(capability.kind) is capability
            )
