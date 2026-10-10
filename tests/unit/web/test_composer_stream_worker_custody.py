import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest

from elspeth.web.async_workers import run_stream_read_in_worker


@pytest.mark.asyncio
async def test_running_sql_survives_repeated_child_cancel_until_actual_completion() -> None:
    entered = Event()
    release = Event()
    completed = Event()

    def sql():
        entered.set()
        try:
            release.wait()
            return "committed-read"
        finally:
            completed.set()

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch("elspeth.web.async_workers._get_shared_executor", autospec=True, return_value=executor),
    ):
        task = asyncio.create_task(run_stream_read_in_worker(sql))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert not completed.is_set()
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert completed.is_set()
        finally:
            release.set()


@pytest.mark.asyncio
async def test_cancelled_queued_future_never_executes_sql() -> None:
    occupied = Event()
    release = Event()
    invoked = Event()

    def occupy():
        occupied.set()
        release.wait()

    def sql():
        invoked.set()
        return "unexpected"

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch("elspeth.web.async_workers._get_shared_executor", autospec=True, return_value=executor),
    ):
        blocker = executor.submit(occupy)
        try:
            while not occupied.is_set():
                await asyncio.sleep(0.01)
            task = asyncio.create_task(run_stream_read_in_worker(sql))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not invoked.is_set()
            release.set()
            blocker.result(timeout=1)
            await asyncio.sleep(0.02)
            assert not invoked.is_set()
        finally:
            release.set()


@pytest.mark.asyncio
async def test_direct_cancelled_sql_frame_child_retains_subscriber_permit() -> None:
    from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamPermits

    entered = Event()
    release = Event()
    completed = Event()
    children = []
    permits = ComposerStreamPermits()
    permit = permits.acquire("owned-operation", "owned-principal")

    def sql():
        entered.set()
        release.wait()
        completed.set()
        return b"data: private\n\n"

    async def frames():
        children.append(asyncio.current_task())
        yield await run_stream_read_in_worker(sql)

    async def authorize():
        return True

    async def receive():
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch("elspeth.web.async_workers._get_shared_executor", autospec=True, return_value=executor),
    ):
        subscriber = asyncio.create_task(
            BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)({}, receive, send)
        )
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            assert children[0] is not None
            children[0].cancel()
            subscriber.cancel()
            with pytest.raises(asyncio.CancelledError):
                await subscriber
            assert permits.occupied == 1
            assert not completed.is_set()
            children[0].cancel()
            await asyncio.sleep(0.02)
            assert permits.occupied == 1
            release.set()
            for _ in range(20):
                await asyncio.sleep(0.01)
                if permits.occupied == 0:
                    break
            assert completed.is_set()
            assert permits.occupied == 0
            assert not any(message["type"] == "http.response.body" and message["body"] for message in sent)
        finally:
            release.set()
