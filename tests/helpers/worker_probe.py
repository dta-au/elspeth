"""A blocking test double that shows whether a synchronous call left the event loop free.

``WorkerProbe.block`` stands in for one synchronous call. It records the thread
it runs on, sets ``entered`` and waits up to ``PROBE_WAIT_SECONDS`` for
``released``. Only a task on the event loop sets ``released``
(``release_when_entered``). ``outcomes`` records ``True`` when that loop task
ran while the call was waiting, and ``False`` when the wait ended without it.
The assertion is about ordering; the five-second bound only stops a test from
waiting forever.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

PROBE_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.01


class WorkerProbe:
    """One blocking stand-in call and what it observed."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self.finished = threading.Event()
        self.outcomes: list[bool] = []
        self.threads: list[int] = []

    def block(self) -> None:
        """Record the calling thread, then wait for the loop to release this call."""
        self.threads.append(threading.get_ident())
        self.entered.set()
        self.outcomes.append(self.released.wait(timeout=PROBE_WAIT_SECONDS))

    def wrap[**P, T](self, func: Callable[P, T]) -> Callable[P, T]:
        """Return ``func`` preceded by ``block``; ``finished`` is set when the call ends."""

        def probed(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                self.block()
                return func(*args, **kwargs)
            finally:
                self.finished.set()

        return probed

    async def wait_entered(self) -> None:
        """Poll from the loop until the probed call has started."""
        deadline = asyncio.get_running_loop().time() + PROBE_WAIT_SECONDS * 2
        while not self.entered.is_set():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the probed call was never entered")
            await asyncio.sleep(_POLL_SECONDS)

    async def release_when_entered(self) -> None:
        """Release the probed call from a loop task once it has started."""
        try:
            await self.wait_entered()
        finally:
            self.released.set()

    async def wait_finished(self) -> None:
        """Poll from the loop until the probed call (and what it delegates to) has returned."""
        deadline = asyncio.get_running_loop().time() + PROBE_WAIT_SECONDS * 2
        while not self.finished.is_set():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the probed call did not finish")
            await asyncio.sleep(_POLL_SECONDS)


async def run_with_probe[T](probe: WorkerProbe, awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` while a loop task releases ``probe`` once it is entered.

    The awaited result or exception is returned or raised unchanged.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        await probe.release_when_entered()
    except BaseException:
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            # Retrieve the outcome so the abandoned task reports nothing later.
            task.exception()
        raise
    return await task
