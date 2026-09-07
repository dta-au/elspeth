"""Escalate required background-worker failure to the process supervisor."""

from __future__ import annotations

import os
import signal


class ProcessRecovery:
    """One shutdown request shared by the process's required asyncio workers.

    Cancelling an ASGI lifespan task does not stop Uvicorn's serving loop.
    SIGTERM reaches the host, which stops accepting requests and drives the
    normal lifespan teardown before exiting for supervisor replacement.
    Calls run on the application's event loop, including ``begin_shutdown``;
    no await splits the check and signal. Once teardown has started, another
    worker failure must not interrupt audited execution cleanup.
    """

    __slots__ = ("_shutdown_started",)

    def __init__(self) -> None:
        self._shutdown_started = False

    def request_shutdown(self) -> None:
        if not self._shutdown_started:
            self._shutdown_started = True
            os.kill(os.getpid(), signal.SIGTERM)

    def begin_shutdown(self) -> None:
        self._shutdown_started = True
