"""Authenticated child endpoint implementing the real recovery owner contract."""

from __future__ import annotations

import asyncio
import os
import threading

from elspeth.composer_mcp.portable_bootstrap import AuthenticatedSupervisor, exchange_sync
from elspeth.contracts.process_watchdog_wire import (
    BootstrapCompletionWitness,
    FrameKind,
    FrameReader,
    GraceAcknowledgement,
    ProcessCompletionWitness,
    ProcessWatchdogTarget,
    RecoveryReason,
    WatchdogFrame,
    WatchdogProtocolError,
    encode_frame,
)
from elspeth.web.process_watchdog import ProcessWatchdogControl, ProcessWatchdogFailure


class PortableProcessWatchdog(ProcessWatchdogControl):
    """One root task, one socket reader and one retained exchange per phase.

    Construct only with connect_supervisor's authenticated channel. Bind the
    actual serving root before worker admission; bootstrap completion is then
    permanently unavailable. A witness is a nominal lifecycle assertion, not
    an introspection substitute for the caller's required joins.
    """

    def __init__(self, connection: AuthenticatedSupervisor, instance_draining: threading.Event) -> None:
        if type(connection) is not AuthenticatedSupervisor:
            raise ProcessWatchdogFailure("Composer endpoint requires authenticated bootstrap")
        channel, target = connection.claim()
        if type(target) is not ProcessWatchdogTarget or target.pid != os.getpid():
            channel.close()
            raise ProcessWatchdogFailure("Composer endpoint target mismatch")
        self._channel = channel
        self._target = target
        self._draining = instance_draining
        self._root: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[WatchdogFrame] | None = None
        self._expected: FrameKind | None = None
        self._exchange_lock = asyncio.Lock()
        self._recovery: asyncio.Task[GraceAcknowledgement] | None = None
        self._completion: asyncio.Task[None] | None = None
        self._disarmed = False
        self._graceful_requested = False

    @property
    def target(self) -> ProcessWatchdogTarget:
        return self._target

    def bind_root_task(self, root: asyncio.Task[None]) -> None:
        if self._root is not None or self._disarmed or root.done() or root.get_loop() is not asyncio.get_running_loop():
            raise ProcessWatchdogFailure("Composer serving root already bound or unavailable")
        self._root = root
        self._reader = asyncio.create_task(self._read_frames(), name="composer-control-reader")

    def assert_watching(self) -> None:
        if self._disarmed or self._root is None or self._reader is None or self._reader.done():
            raise ProcessWatchdogFailure("Required Composer supervision is not active")

    async def _read_frames(self) -> None:
        reader = FrameReader()
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.sock_recv(self._channel, reader.read_capacity)
                if not data:
                    raise ProcessWatchdogFailure("Private Composer supervisor channel closed")
                for frame in reader.feed(data):
                    frame.validate_target(nonce=self.target.nonce, target_pid=self.target.pid)
                    if frame.kind is FrameKind.SHUTDOWN_REQUEST:
                        self.request_graceful_termination()
                    elif self._pending is not None and not self._pending.done() and frame.kind is self._expected:
                        self._pending.set_result(frame)
                        if frame.kind is FrameKind.DISARM_ACK:
                            self._disarmed = True
                            return
                    else:
                        raise WatchdogProtocolError("Unexpected Composer control reply")
        except (OSError, WatchdogProtocolError, ProcessWatchdogFailure) as error:
            failure = ProcessWatchdogFailure("Private Composer supervisor channel failed")
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(failure)
            raise failure from error
        finally:
            self._channel.close()

    async def _request(self, frame: WatchdogFrame, expected: FrameKind) -> WatchdogFrame:
        async with self._exchange_lock:
            self.assert_watching()
            self._pending = asyncio.get_running_loop().create_future()
            self._expected = expected
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.get_running_loop().sock_sendall(self._channel, encode_frame(frame))
                    return await self._pending
            except (OSError, TimeoutError) as error:
                self._channel.close()
                raise ProcessWatchdogFailure("Private Composer exchange failed") from error
            finally:
                self._pending = None
                self._expected = None

    async def _begin_recovery(self, reason: RecoveryReason) -> GraceAcknowledgement:
        frame = await self._request(
            WatchdogFrame(FrameKind.BEGIN_RECOVERY, self.target.nonce, self.target.pid, reason), FrameKind.GRACE_ACK
        )
        assert frame.armed_at_ns is not None and frame.deadline_ns is not None
        return GraceAcknowledgement(self.target, frame.armed_at_ns, frame.deadline_ns)

    async def begin_recovery(self, reason: RecoveryReason) -> GraceAcknowledgement:
        self._draining.set()
        if self._recovery is None:
            self.assert_watching()
            self._recovery = asyncio.create_task(self._begin_recovery(reason), name="composer-recovery-exchange")
        # Cancellation never transfers reader ownership or discards a late ACK.
        return await asyncio.shield(self._recovery)

    async def _complete(self) -> None:
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        await self._request(WatchdogFrame(FrameKind.COMPLETE, self.target.nonce, self.target.pid), FrameKind.DISARM_ACK)
        assert self._reader is not None
        await asyncio.shield(self._reader)

    async def complete(self, witness: ProcessCompletionWitness) -> None:
        if type(witness) is not ProcessCompletionWitness or witness.target != self.target:
            raise ProcessWatchdogFailure("Composer completion witness mismatch")
        if self._completion is None:
            self.assert_watching()
            self._completion = asyncio.create_task(self._complete(), name="composer-completion-exchange")
        await asyncio.shield(self._completion)

    def complete_bootstrap(self, witness: BootstrapCompletionWitness) -> None:
        if type(witness) is not BootstrapCompletionWitness or witness.target != self.target or self._root is not None or self._disarmed:
            raise ProcessWatchdogFailure("Composer bootstrap completion witness or phase mismatch")
        try:
            reply = exchange_sync(self._channel, WatchdogFrame(FrameKind.COMPLETE, self.target.nonce, self.target.pid))
            if reply.kind is not FrameKind.DISARM_ACK:
                raise ProcessWatchdogFailure("Composer bootstrap completion refused")
            self._disarmed = True
        finally:
            self._channel.close()

    def abort_bootstrap(self, reason: RecoveryReason, witness: BootstrapCompletionWitness | None = None) -> None:
        if reason is not RecoveryReason.FAILED_STARTUP or self._root is not None:
            raise ProcessWatchdogFailure("Composer bootstrap abort phase mismatch")
        if witness is not None:
            self.complete_bootstrap(witness)
            return
        self._draining.set()
        try:
            reply = exchange_sync(self._channel, WatchdogFrame(FrameKind.BEGIN_RECOVERY, self.target.nonce, self.target.pid, reason))
            if reply.kind is not FrameKind.GRACE_ACK:
                raise ProcessWatchdogFailure("Composer bootstrap recovery refused")
        except BaseException:
            self.force_termination()
            raise

    async def observe_helper_outcome(self) -> None:
        self.assert_watching()
        assert self._reader is not None
        await asyncio.shield(self._reader)

    def request_graceful_termination(self) -> None:
        if self._root is None:
            raise ProcessWatchdogFailure("Composer serving root is not bound")
        self._draining.set()
        if not self._graceful_requested:
            self._graceful_requested = True
            self._root.cancel("Composer process shutdown")

    def force_termination(self) -> None:
        # Self-only authority; independent parent still enforces a stuck loop.
        os._exit(70)
