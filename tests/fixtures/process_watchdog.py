"""Nominal watchdog test double: no subprocess, pidfd or signal authority."""

from __future__ import annotations

import asyncio
import signal
import threading
from uuid import uuid4

from elspeth.web.process_watchdog import (
    BootstrapCompletionWitness,
    GraceAcknowledgement,
    ProcessCompletionWitness,
    ProcessWatchdogControl,
    ProcessWatchdogFailure,
    ProcessWatchdogTarget,
)
from elspeth.web.process_watchdog_codec import GRACE_NS, RecoveryReason


class OwnedTestProcessWatchdog(ProcessWatchdogControl):
    def __init__(self, instance_draining: threading.Event) -> None:
        self._target = ProcessWatchdogTarget(1, uuid4().hex + uuid4().hex)
        self.draining = instance_draining
        self.signals: list[signal.Signals] = []
        self.reasons: list[RecoveryReason] = []
        self.completed = False
        self.failure: BaseException | None = None
        self._outcome = asyncio.Event()
        self._ack: GraceAcknowledgement | None = None

    @property
    def target(self) -> ProcessWatchdogTarget:
        return self._target

    def assert_watching(self) -> None:
        if self.completed or self.failure is not None:
            raise ProcessWatchdogFailure("Owned fake watchdog is unavailable")

    async def begin_recovery(self, reason: RecoveryReason) -> GraceAcknowledgement:
        self.assert_watching()
        self.draining.set()
        self.reasons.append(reason)
        if self._ack is None:
            self._ack = GraceAcknowledgement(self.target, 1, 1 + GRACE_NS)
        return self._ack

    async def complete(self, witness: ProcessCompletionWitness) -> None:
        if type(witness) is not ProcessCompletionWitness or witness.target != self.target:
            raise ProcessWatchdogFailure("Owned completion witness mismatch")
        self.completed = True
        self._outcome.set()

    def complete_bootstrap(self, witness: BootstrapCompletionWitness) -> None:
        if type(witness) is not BootstrapCompletionWitness or witness.target != self.target:
            raise ProcessWatchdogFailure("Owned bootstrap witness mismatch")
        self.completed = True
        self._outcome.set()

    def abort_bootstrap(self, reason: RecoveryReason, witness: BootstrapCompletionWitness | None = None) -> None:
        if reason is not RecoveryReason.FAILED_STARTUP:
            raise ProcessWatchdogFailure("Owned bootstrap reason mismatch")
        if witness is not None:
            self.complete_bootstrap(witness)
        else:
            self.draining.set()
            self.reasons.append(reason)
            self._ack = GraceAcknowledgement(self.target, 1, 1 + GRACE_NS)

    async def observe_helper_outcome(self) -> None:
        await self._outcome.wait()
        if self.failure is not None:
            raise self.failure

    def request_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)

    def fail(self, error: BaseException) -> None:
        self.failure = error
        self._outcome.set()
