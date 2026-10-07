"""Diagnostic-only physical app finalizer terminal and strict framing."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future
from threading import Event

PREFIX = "ACTUAL_APP_FINALIZER_TERMINAL "
RECEIPTS = ("counter_returned", "callback_exit_receipt", "generation_joined")
BOOLEAN_FIELDS = (
    "private_future_result_ok",
    "generation_thread_joined",
    "callback_entered",
    "future_done",
    "future_bound_exact",
    "reservation_released",
    "reservation_registered_exact",
    "invocation_exited",
    "owner_registered",
    "owner_claimed",
    "registry_join_returned",
    "counter_returned",
    "callback_exit_receipt",
    "generation_joined",
    "getter",
)
INTEGER_FIELDS = ("callback_original_occurrences", "physical_failure_total")
OPTIONAL_ERROR_FIELDS = ("drain_error", "observer_error", "terminal_observer_error")
ALL_FIELDS = frozenset(("scenario", *BOOLEAN_FIELDS, *INTEGER_FIELDS, *OPTIONAL_ERROR_FIELDS))
PHYSICAL_PREREQUISITES = (
    "private_future_result_ok",
    "generation_thread_joined",
    "callback_entered",
    "future_done",
    "future_bound_exact",
    "reservation_released",
    "reservation_registered_exact",
    "invocation_exited",
    "owner_registered",
    "owner_claimed",
    "registry_join_returned",
)


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("app terminal duplicate JSON member")
        result[name] = value
    return result


def parse_terminal(raw: str) -> dict[str, object] | None:
    lines = raw.splitlines(keepends=True)
    framed = [line for line in lines if line.startswith(PREFIX)]
    if not framed:
        return None
    if len(framed) != 1 or not framed[0].endswith("\n"):
        raise ValueError("app terminal frame missing or duplicated")
    parsed = json.loads(framed[0][len(PREFIX) : -1], object_pairs_hook=_unique_members)
    if type(parsed) is not dict or set(parsed) != ALL_FIELDS:
        raise ValueError("app terminal shape changed")
    if parsed["scenario"] not in ("returned", "partial") or type(parsed["scenario"]) is not str:
        raise ValueError("app terminal scenario changed")
    if any(type(parsed[name]) is not bool for name in BOOLEAN_FIELDS):
        raise ValueError("app terminal boolean changed")
    if any(type(parsed[name]) is not int or parsed[name] < 0 for name in INTEGER_FIELDS):
        raise ValueError("app terminal counter changed")
    if any(parsed[name] is not None and type(parsed[name]) is not str for name in OPTIONAL_ERROR_FIELDS):
        raise ValueError("app terminal error type changed")
    return parsed


def classify(snapshot: dict[str, object]) -> str | None:
    if any(snapshot[name] is not None for name in OPTIONAL_ERROR_FIELDS):
        return None
    if any(snapshot[name] is not True for name in PHYSICAL_PREREQUISITES):
        return None
    if snapshot["callback_original_occurrences"] != 1 or snapshot["physical_failure_total"] != 1:
        return None
    missing = [name for name in RECEIPTS if snapshot[name] is False]
    if snapshot["getter"] is True:
        return None
    if len(missing) > 1:
        return None
    if not missing:
        return "getter"
    return {
        "counter_returned": "counter",
        "callback_exit_receipt": "callback",
        "generation_joined": "generation",
    }[missing[0]]


def exactly_selected(snapshot: dict[str, object], selected: str) -> bool:
    return selected in {"counter", "callback", "generation", "getter"} and classify(snapshot) == selected


def healthy_complete(snapshot: dict[str, object]) -> bool:
    return (
        all(snapshot[name] is True for name in PHYSICAL_PREREQUISITES)
        and all(snapshot[name] is None for name in OPTIONAL_ERROR_FIELDS)
        and snapshot["callback_original_occurrences"] == 1
        and snapshot["physical_failure_total"] == 1
        and all(snapshot[name] is True for name in RECEIPTS)
        and snapshot["getter"] is True
    )


def expected_drain_negative(snapshot: dict[str, object]) -> bool:
    return (
        all(snapshot[name] is True for name in PHYSICAL_PREREQUISITES)
        and snapshot["callback_original_occurrences"] == 1
        and snapshot["physical_failure_total"] == 1
        and snapshot["drain_error"] == "RequiredInvocationIntegrityError"
        and snapshot["observer_error"] is None
        and snapshot["terminal_observer_error"] is None
        and snapshot["counter_returned"] is True
        and snapshot["callback_exit_receipt"] is True
        and classify(snapshot) is None
    )


class ActualAppReceiptTerminalObserver:
    """Publish one read-only terminal after exact private and generation joins."""

    def __init__(
        self,
        *,
        scenario: str,
        registry,
        capability,
        private_future: Future[None],
        callback_original: BaseException,
        callback_failed: Event,
        expected_generation,
    ) -> None:
        if scenario not in ("returned", "partial"):
            raise ValueError("unknown app receipt scenario")
        self.scenario = scenario
        self.registry = registry
        self.capability = capability
        self.private_future = private_future
        self.callback_original = callback_original
        self.callback_failed = callback_failed
        self.expected_generation = expected_generation
        self.stop_requested = Event()
        self.emitted = Event()
        self.originals: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, name=f"actual-app-receipt-{scenario}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop_and_join(self) -> None:
        self.stop_requested.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                raise AssertionError("actual app receipt observer thread remained live")
        if self.originals:
            raise BaseExceptionGroup("actual app receipt observer originals", self.originals)

    def _run(self) -> None:
        try:
            while not self.stop_requested.is_set():
                if not self.callback_failed.is_set():
                    time.sleep(0.005)
                    continue
                reservation = self.capability._physical_reservation
                if reservation is None:
                    time.sleep(0.005)
                    continue
                generation = reservation._registering_generation
                if generation is None or generation is not self.expected_generation:
                    raise AssertionError("actual app finalizer generation identity changed")
                drain = generation.drain_thread
                if drain is None or drain.ident is None:
                    time.sleep(0.005)
                    continue
                drain.join(timeout=0.005)
                if drain.is_alive():
                    continue
                drain.join(timeout=0)
                if drain.is_alive() or not self.private_future.done():
                    time.sleep(0.005)
                    continue
                # Neither Future.done nor generation.joined alone is a physical
                # outcome. Read the actual private result after the thread join.
                self.private_future.result()
                trace = reservation.witness.snapshot()
                failures = self.capability.physical_failure_originals
                with self.registry._lock:
                    returned = self.registry._executor_join_returned
                snapshot = {
                    "scenario": self.scenario,
                    "private_future_result_ok": True,
                    "generation_thread_joined": True,
                    "callback_entered": self.callback_failed.is_set(),
                    "future_done": reservation.future is not None and reservation.future.done(),
                    "future_bound_exact": reservation.future is self.capability._physical_future,
                    "reservation_released": reservation.released,
                    "reservation_registered_exact": reservation._registering_generation is generation,
                    "invocation_exited": not trace.impossible and trace.callable_started and trace.callable_finished and trace.exited,
                    "owner_registered": self.registry.owner.owns_registered(self.capability),
                    "owner_claimed": self.registry.owner.owns_claimed(self.capability),
                    "registry_join_returned": returned,
                    "counter_returned": self.capability._physical_admission_release_return_observed,
                    "callback_exit_receipt": self.capability._physical_callback_failure_exit_observed,
                    "generation_joined": generation.joined.is_set(),
                    "getter": self.registry.executor_join_physically_observed,
                    "callback_original_occurrences": sum(original is self.callback_original for original in failures),
                    "physical_failure_total": len(failures),
                    "drain_error": None if generation.drain_error is None else type(generation.drain_error).__name__,
                    "observer_error": None if generation.observer_error is None else type(generation.observer_error).__name__,
                    "terminal_observer_error": None,
                }
                os.write(1, (PREFIX + json.dumps(snapshot, sort_keys=True) + "\n").encode())
                self.emitted.set()
                return
        except BaseException as original:
            self.originals.append(original)
            try:
                os.write(1, f"ACTUAL_APP_TERMINAL_OBSERVER_ERROR {type(original).__name__}\n".encode())
            except BaseException as publication_original:
                self.originals.append(publication_original)
