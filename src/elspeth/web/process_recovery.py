"""One process draining owner; independent supervision precedes self termination."""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import TYPE_CHECKING

from elspeth.web.process_watchdog import ProcessWatchdogControl
from elspeth.web.process_watchdog_codec import RecoveryReason

if TYPE_CHECKING:
    from elspeth.web.async_workers import RequiredGenerationDrainExpired


class ProcessRecovery:
    def __init__(self, *, watchdog: ProcessWatchdogControl, instance_draining: threading.Event) -> None:
        if not isinstance(watchdog, ProcessWatchdogControl):
            raise TypeError("Process recovery requires an owned watchdog")
        self.watchdog = watchdog
        self.instance_draining = instance_draining
        self._shutdown_started = False
        self._escalation_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._observations: list[RequiredGenerationDrainExpired] = []

    def start_monitor(self) -> None:
        self.watchdog.assert_watching()
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor(), name="process-watchdog-monitor")

    async def _monitor(self) -> None:
        try:
            await self.watchdog.observe_helper_outcome()
        except BaseException:
            self.instance_draining.set()
            self.watchdog.request_signal(signal.SIGKILL)
            raise

    async def _escalate(self, reason: RecoveryReason, *, signal_target: bool) -> None:
        try:
            await self.watchdog.begin_recovery(reason)
        except BaseException:
            self.watchdog.request_signal(signal.SIGKILL)
            raise
        if signal_target:
            self.watchdog.request_signal(signal.SIGTERM)

    def _begin(self, reason: RecoveryReason, *, signal_target: bool) -> None:
        self.instance_draining.set()
        if not self._shutdown_started:
            self._shutdown_started = True
            self._escalation_task = asyncio.create_task(
                self._escalate(reason, signal_target=signal_target), name="process-recovery-escalation"
            )

    def request_shutdown(self) -> None:
        self._begin(RecoveryReason.REQUIRED_WORKER_LOST, signal_target=True)

    def request_startup_failure(self) -> None:
        self._begin(RecoveryReason.FAILED_STARTUP, signal_target=False)

    def begin_shutdown(self) -> None:
        self._begin(RecoveryReason.NORMAL_SHUTDOWN, signal_target=False)

    def required_generation_expired(self, observation: RequiredGenerationDrainExpired) -> None:
        self._observations.append(observation)
        self._begin(RecoveryReason.REQUIRED_GENERATION_UNRESOLVED, signal_target=True)

    async def _join_owned(self, task: asyncio.Task[None]) -> None:
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except BaseException:
                break
        try:
            task.result()
        except BaseException as failure:
            if cancellation is not None and failure is not cancellation:
                raise BaseExceptionGroup("Process recovery join and caller cancellation failed", [failure, cancellation]) from None
            raise
        if cancellation is not None:
            raise cancellation

    async def join_escalation(self) -> None:
        if self._escalation_task is not None:
            await self._join_owned(self._escalation_task)

    async def join_monitor_after_completion(self) -> None:
        if self._monitor_task is not None:
            await self._join_owned(self._monitor_task)
