"""Explicit recording recovery owner for shared-executor unit harnesses."""

from __future__ import annotations

from elspeth.web.required_executor import RequiredExecutorGenerationCustodian, RequiredGenerationDrainExpired


class RecordingRequiredGenerationRecovery:
    def __init__(self) -> None:
        self.observations: list[RequiredGenerationDrainExpired] = []

    def __call__(self, observation: RequiredGenerationDrainExpired) -> None:
        if not isinstance(observation, RequiredGenerationDrainExpired):
            raise TypeError("Expected owned generation observation")
        self.observations.append(observation)


def assert_owned_executor_fixture_settled(generation: RequiredExecutorGenerationCustodian | None) -> None:
    """Refuse to erase unresolved physical custody; never perform cleanup."""
    if generation is None:
        return
    failures: list[BaseException] = []
    with generation.submission_lock:
        for reservation in generation.reservations.values():
            if reservation.held and not reservation.released:
                failures.append(AssertionError("Executor fixture retained charged invocation"))
            if reservation.future is not None and not reservation.future.done():
                failures.append(AssertionError("Executor fixture retained incomplete actual Future"))
        if generation.state == "quarantined":
            if not generation.joined.is_set() or not generation.recovery_finished.is_set():
                failures.append(AssertionError("Executor fixture retained unjoined quarantined generation"))
            if generation.observer is None or not generation.observer.done():
                failures.append(AssertionError("Executor fixture retained unfinished recovery observer"))
        if failures:
            if generation.drain_error is not None:
                failures.append(generation.drain_error)
            if generation.observer_error is not None:
                failures.append(generation.observer_error)
    if failures:
        raise BaseExceptionGroup("Executor fixture cannot discard unresolved custody", failures)
