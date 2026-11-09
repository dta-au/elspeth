"""Application-owned Linux process watchdog with private bounded control."""

from __future__ import annotations

import asyncio
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import final

from elspeth.web.process_watchdog_codec import (
    FrameKind,
    FrameReader,
    RecoveryReason,
    WatchdogFrame,
    WatchdogProtocolError,
    encode_frame,
)

WEB_PROCESS_RECOVERY_GRACE_SECONDS = 30.0


class ProcessWatchdogFailure(RuntimeError):
    """Required private process supervision failed."""


@final
@dataclass(frozen=True, slots=True)
class ProcessWatchdogTarget:
    pid: int
    nonce: str


@final
@dataclass(frozen=True, slots=True)
class GraceAcknowledgement:
    target: ProcessWatchdogTarget
    armed_at_ns: int
    deadline_ns: int


@final
@dataclass(frozen=True, slots=True)
class ProcessCompletionWitness:
    """Owned lifecycle creates this only after all joins and engine finalization."""

    target: ProcessWatchdogTarget


@final
@dataclass(frozen=True, slots=True)
class BootstrapCompletionWitness:
    """Owned factory creates this only while no construction effects entered."""

    target: ProcessWatchdogTarget


class ProcessWatchdogControl(ABC):
    @property
    @abstractmethod
    def target(self) -> ProcessWatchdogTarget: ...

    @abstractmethod
    def assert_watching(self) -> None: ...

    @abstractmethod
    async def begin_recovery(self, reason: RecoveryReason) -> GraceAcknowledgement: ...

    @abstractmethod
    async def complete(self, witness: ProcessCompletionWitness) -> None: ...

    @abstractmethod
    def complete_bootstrap(self, witness: BootstrapCompletionWitness) -> None: ...

    @abstractmethod
    def abort_bootstrap(self, reason: RecoveryReason, witness: BootstrapCompletionWitness | None = None) -> None: ...

    @abstractmethod
    async def observe_helper_outcome(self) -> None: ...

    @abstractmethod
    def request_signal(self, sig: signal.Signals) -> None: ...


_active_watchdog: ProcessWatchdog | None = None
_owner_lock = threading.Lock()


class ProcessWatchdog(ProcessWatchdogControl):
    def __init__(self, instance_draining: threading.Event) -> None:
        global _active_watchdog
        with _owner_lock:
            if _active_watchdog is not None:
                raise ProcessWatchdogFailure("A serving process already owns its watchdog")
            self._draining = instance_draining
            self._target = ProcessWatchdogTarget(os.getpid(), secrets.token_hex(32))
            self._pidfd = os.pidfd_open(self._target.pid, 0)
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            self._channel = parent
            self._channel.setblocking(False)
            self._reader = FrameReader()
            self._async_lock = asyncio.Lock()
            self._disarmed = False
            self._ack: GraceAcknowledgement | None = None
            self._helper: subprocess.Popen[bytes] | None = None
            try:
                signal.pidfd_send_signal(self._pidfd, 0)
                helper = Path(__file__).with_name("process_watchdog_helper.py")
                self._helper = subprocess.Popen(
                    [sys.executable, str(helper), str(child.fileno()), str(self._pidfd)],
                    close_fds=True,
                    pass_fds=(child.fileno(), self._pidfd),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                child.close()
                hello = WatchdogFrame(FrameKind.HELLO, self._target.nonce, self._target.pid)
                frame = self._exchange_sync(hello, timeout=2.0)
                if frame.kind is not FrameKind.WATCHING_ACK:
                    raise ProcessWatchdogFailure("Watchdog startup acknowledgement refused")
            except BaseException:
                child.close()
                parent.close()
                os.close(self._pidfd)
                if self._helper is not None and self._helper.poll() is None:
                    self._helper.terminate()
                    try:
                        self._helper.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        self._helper.kill()
                        self._helper.wait(timeout=1.0)
                raise
            _active_watchdog = self

    @property
    def target(self) -> ProcessWatchdogTarget:
        return self._target

    def assert_watching(self) -> None:
        if self._disarmed or self._helper is None or self._helper.poll() is not None:
            raise ProcessWatchdogFailure("Required watchdog is not active")

    def _validate(self, frame: WatchdogFrame) -> WatchdogFrame:
        frame.validate_target(nonce=self._target.nonce, target_pid=self._target.pid)
        return frame

    def _exchange_sync(self, frame: WatchdogFrame, *, timeout: float) -> WatchdogFrame:
        data = memoryview(encode_frame(frame))
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self._channel, selectors.EVENT_READ | selectors.EVENT_WRITE)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessWatchdogFailure("Private watchdog acknowledgement timed out")
                for _, events in selector.select(remaining):
                    if events & selectors.EVENT_WRITE and data:
                        count = self._channel.send(data)
                        data = data[count:]
                        if not data:
                            selector.modify(self._channel, selectors.EVENT_READ)
                    if events & selectors.EVENT_READ:
                        raw = self._channel.recv(self._reader.read_capacity)
                        if not raw:
                            raise ProcessWatchdogFailure("Private watchdog channel closed")
                        frames = self._reader.feed(raw)
                        if frames:
                            if len(frames) != 1 or data:
                                raise WatchdogProtocolError("Unsolicited watchdog acknowledgement")
                            return self._validate(frames[0])

    async def _exchange(self, frame: WatchdogFrame) -> WatchdogFrame:
        async with self._async_lock:
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(1.0):
                await loop.sock_sendall(self._channel, encode_frame(frame))
                while True:
                    raw = await loop.sock_recv(self._channel, self._reader.read_capacity)
                    if not raw:
                        raise ProcessWatchdogFailure("Private watchdog channel closed")
                    frames = self._reader.feed(raw)
                    if frames:
                        if len(frames) != 1:
                            raise WatchdogProtocolError("Unsolicited watchdog acknowledgement")
                        return self._validate(frames[0])

    def _retain_ack(self, frame: WatchdogFrame) -> GraceAcknowledgement:
        if frame.kind is not FrameKind.GRACE_ACK or frame.armed_at_ns is None or frame.deadline_ns is None:
            raise ProcessWatchdogFailure("Private recovery acknowledgement refused")
        ack = GraceAcknowledgement(self.target, frame.armed_at_ns, frame.deadline_ns)
        if self._ack is not None and self._ack != ack:
            raise ProcessWatchdogFailure("Private recovery deadline changed")
        self._ack = ack
        return ack

    async def begin_recovery(self, reason: RecoveryReason) -> GraceAcknowledgement:
        self._draining.set()
        if self._ack is not None:
            return self._ack
        self.assert_watching()
        return self._retain_ack(await self._exchange(WatchdogFrame(FrameKind.BEGIN_RECOVERY, self.target.nonce, self.target.pid, reason)))

    def _validate_completion(self, witness: ProcessCompletionWitness | BootstrapCompletionWitness) -> None:
        if type(witness) not in (ProcessCompletionWitness, BootstrapCompletionWitness) or witness.target != self.target:
            raise ProcessWatchdogFailure("Completion witness belongs to another process owner")

    async def complete(self, witness: ProcessCompletionWitness) -> None:
        self._validate_completion(witness)
        if self._disarmed:
            return
        frame = await self._exchange(WatchdogFrame(FrameKind.COMPLETE, self.target.nonce, self.target.pid))
        if frame.kind is not FrameKind.DISARM_ACK:
            raise ProcessWatchdogFailure("Private completion acknowledgement refused")
        self._disarmed = True
        self._channel.close()
        assert self._helper is not None
        deadline = time.monotonic() + 1.0
        while self._helper.poll() is None:
            if time.monotonic() >= deadline:
                raise ProcessWatchdogFailure("Completed watchdog has not exited")
            await asyncio.sleep(0.01)
        os.close(self._pidfd)

    def complete_bootstrap(self, witness: BootstrapCompletionWitness) -> None:
        self._validate_completion(witness)
        frame = self._exchange_sync(WatchdogFrame(FrameKind.COMPLETE, self.target.nonce, self.target.pid), timeout=1.0)
        if frame.kind is not FrameKind.DISARM_ACK:
            raise ProcessWatchdogFailure("Private bootstrap completion refused")
        self._disarmed = True
        self._channel.close()
        assert self._helper is not None
        self._helper.wait(timeout=1.0)
        os.close(self._pidfd)

    def abort_bootstrap(self, reason: RecoveryReason, witness: BootstrapCompletionWitness | None = None) -> None:
        if reason is not RecoveryReason.FAILED_STARTUP:
            raise ProcessWatchdogFailure("Bootstrap abort requires exact failed-startup reason")
        if witness is not None:
            self.complete_bootstrap(witness)
            return
        self._draining.set()
        try:
            self._retain_ack(
                self._exchange_sync(WatchdogFrame(FrameKind.BEGIN_RECOVERY, self.target.nonce, self.target.pid, reason), timeout=1.0)
            )
        except BaseException:
            self.request_signal(signal.SIGKILL)
            raise

    async def observe_helper_outcome(self) -> None:
        assert self._helper is not None
        while self._helper.poll() is None:
            if not self._disarmed and not self._async_lock.locked():
                try:
                    pending = self._channel.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    pass
                except OSError as error:
                    raise ProcessWatchdogFailure("Private process watchdog channel failed") from error
                else:
                    if not pending:
                        raise ProcessWatchdogFailure("Private process watchdog channel closed")
                    raise ProcessWatchdogFailure("Unsolicited private process watchdog frame")
            await asyncio.sleep(0.05)
        if not self._disarmed:
            raise ProcessWatchdogFailure("Required process watchdog exited while owner remained active")

    def request_signal(self, sig: signal.Signals) -> None:
        if type(sig) is not signal.Signals or sig not in (signal.SIGTERM, signal.SIGKILL):
            raise ProcessWatchdogFailure("Unapproved process recovery signal")
        signal.pidfd_send_signal(self._pidfd, sig)


type ProcessWatchdogFactory = Callable[[threading.Event], ProcessWatchdogControl]


def create_process_watchdog(instance_draining: threading.Event) -> ProcessWatchdogControl:
    return ProcessWatchdog(instance_draining)
