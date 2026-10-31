"""Uvicorn process ownership for simultaneous HTTP and Composer draining."""

from __future__ import annotations

import asyncio
import socket
import time
from functools import partial
from types import FrameType

import uvicorn
from uvicorn.supervisors import ChangeReload

from elspeth.web.config import settings_from_env
from elspeth.web.sessions.composer_drain import ComposerDrainSignal


class ComposerServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        # Construct process resources inside serve, including in a reload
        # child. The parent Server remains picklable for uvicorn's spawn path.
        self.drain_signal: ComposerDrainSignal | None = None
        self._http_drain_seconds: float | None = None

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        from elspeth.web.app import create_app

        self.drain_signal = ComposerDrainSignal()
        self._http_drain_seconds = settings_from_env().web_http_drain_seconds
        # Uvicorn's public configuration accepts integer seconds. The actual
        # float H bound is owned below, using the original signal's clock;
        # no rounding can silently extend or shorten that process deadline.
        self.config.timeout_graceful_shutdown = None
        self.config.app = partial(create_app, drain_signal=self.drain_signal)
        await super().serve(sockets=sockets)

    async def _wait_tasks_to_complete(self) -> None:
        signal, duration = self.drain_signal, self._http_drain_seconds
        if signal is None or duration is None:
            raise RuntimeError("Composer HTTP drain lost its actual process timing owner")
        now = time.monotonic()
        started = now if signal.started_at is None else signal.started_at
        remaining = max(0.0, started + duration - now)
        # Server.shutdown already handles TimeoutError by cancelling actual
        # HTTP tasks and then joining the application lifespan. Preserve that
        # path while accounting for time spent since the signal, including
        # Uvicorn's connection-close setup before this method is called.
        async with asyncio.timeout(remaining):
            await super()._wait_tasks_to_complete()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.drain_signal is not None:
            self.drain_signal.request()
        super().handle_exit(sig, frame)


def run_composer_server(config: uvicorn.Config) -> None:
    """Keep the CLI's reload support while owning the actual child Server."""
    server = ComposerServer(config)
    if config.should_reload:
        sock = config.bind_socket()
        try:
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        finally:
            sock.close()
    else:
        server.run()
        if not server.started:
            raise SystemExit(3)
