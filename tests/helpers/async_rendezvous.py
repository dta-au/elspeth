"""Wait for a test rendezvous without hiding the task that is meant to reach it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

# A hang guard, not a performance budget. The rendezvous points these tests
# wait for sit behind real SQLite work on the shared worker pool: every
# statement may wait up to the 5 s busy_timeout and every worker hop up to 1 s
# for admission, so a loaded host can take several seconds and still be
# correct. 30 s sits above those bounds; reaching it means the producer is
# stuck, not slow.
RENDEZVOUS_HANG_GUARD_SECONDS = 30.0


async def wait_for_event_or_producer[T](
    event: asyncio.Event,
    producer: asyncio.Future[T],
    *,
    waiting_for: str,
    explain: Callable[[], Awaitable[str]] | None = None,
) -> None:
    """Return once ``event`` is set; fail with the producer's outcome if it ends first.

    ``producer`` is the task whose progress sets ``event``. If it finishes
    before setting it, its own exception is re-raised at once (or its result
    reported, with ``explain()`` appended when given) instead of a bare
    ``TimeoutError`` after the guard. The caller keeps ownership of the
    producer: it is never cancelled here.
    """
    if not event.is_set():
        waiter = asyncio.ensure_future(event.wait())
        watched: set[asyncio.Future[Any]] = {waiter, producer}
        try:
            await asyncio.wait(watched, timeout=RENDEZVOUS_HANG_GUARD_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
    if event.is_set():
        return
    if not producer.done():
        pytest.fail(
            f"{waiting_for}: not reached, and its producer did not finish, within the {RENDEZVOUS_HANG_GUARD_SECONDS:g} s hang guard"
        )
    if producer.cancelled():
        pytest.fail(f"{waiting_for}: not reached; its producer was cancelled first")
    failure = producer.exception()
    if failure is not None:
        failure.add_note(f"Raised by the producer before the test rendezvous: {waiting_for}")
        raise failure
    result = producer.result()
    outcome = f"{result.status_code} {result.text}" if isinstance(result, httpx.Response) else repr(result)
    detail = f"; {await explain()}" if explain is not None else ""
    pytest.fail(f"{waiting_for}: not reached; its producer finished first with {outcome}{detail}")
