"""Exact generation completion identity and bounded idempotence controls."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from elspeth.web.required_executor import (
    InvocationReservation,
    RequiredExecutorGenerationCustodian,
    RequiredInvocationIntegrityError,
    RequiredInvocationWitness,
)


def generation(executor: ThreadPoolExecutor) -> RequiredExecutorGenerationCustodian:
    return RequiredExecutorGenerationCustodian(
        executor,
        generation_identity=401,
        drain_seconds=1,
        generation_unavailable=Event(),
        instance_draining=Event(),
        recovery_callback=lambda _observation: None,
        replacement_factory=lambda: ThreadPoolExecutor(max_workers=1),
        install_replacement=lambda replacement: replacement.shutdown(wait=True),
    )


def test_repeated_actual_future_completion_keeps_exact_once_release_without_retained_registry() -> None:
    releases: list[bool] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        custody = generation(executor)
        reservation = InvocationReservation(RequiredInvocationWitness(401), lambda: releases.append(True))
        reservation.held = True
        future = Future()
        future.set_result(None)
        reservation.future = future
        custody.register(reservation)
        reservation.release_future(future)
        reservation.release_future(future)
        custody.record_completed(reservation)
        assert releases == [True]
        assert custody.reservations == {}


@pytest.mark.parametrize("fault", ["unregistered", "foreign", "missing", "replaced"])
def test_completion_refuses_unowned_or_corrupted_registered_identity(fault: str) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        custody = generation(executor)
        reservation = InvocationReservation(RequiredInvocationWitness(401), lambda: None)
        reservation.released = True
        if fault == "foreign":
            generation(executor).register(reservation)
        elif fault in {"missing", "replaced"}:
            custody.register(reservation)
            if fault == "missing":
                del custody.reservations[reservation.witness.identity]
            else:
                custody.reservations[reservation.witness.identity] = InvocationReservation(RequiredInvocationWitness(401), lambda: None)
        with pytest.raises(RequiredInvocationIntegrityError):
            custody.record_completed(reservation)
        assert not reservation._completion_recorded


def test_duplicate_generation_registration_is_an_integrity_failure() -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        custody = generation(executor)
        reservation = InvocationReservation(RequiredInvocationWitness(401), lambda: None)
        custody.register(reservation)
        with pytest.raises(RequiredInvocationIntegrityError):
            custody.register(reservation)
        with pytest.raises(RequiredInvocationIntegrityError):
            generation(executor).register(reservation)
        assert custody.reservations[reservation.witness.identity] is reservation
