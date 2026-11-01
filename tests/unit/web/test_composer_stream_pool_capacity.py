from __future__ import annotations

import asyncio
from threading import Event, Lock
from uuid import uuid4

import pytest

from elspeth.web import async_workers
from elspeth.web.async_workers import (
    AsyncWorkerAdmissionTimeoutError,
    outstanding_admissions,
    run_stream_read_in_worker,
)
from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamCapacityError, ComposerStreamPermits
from tests.helpers.continuation_app import ScriptedSDK, assistant_contents, continuation_app, end_admit, install_sdk, settle


async def _wait_for(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_eight_hung_stream_reads_leave_shared_pool_for_real_auth_and_terminal_sql(tmp_path, monkeypatch) -> None:
    sdk = ScriptedSDK([("primary", ("Completed delayed response.", None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    permits = ComposerStreamPermits()
    release = Event()
    entered = [Event() for _ in range(8)]
    finished = [Event() for _ in range(8)]
    subscribers = []
    assert outstanding_admissions() == 0
    executor = async_workers._get_shared_executor()
    generation = async_workers._generation_for(executor)
    assert executor._max_workers == 16
    assert async_workers._SHARED_EXECUTOR is executor
    assert async_workers._GENERATION_CUSTODIAN is generation

    async def authorize():
        return True

    async def receive():
        await asyncio.Event().wait()

    async def send(_message):
        return None

    async with continuation_app(tmp_path, monkeypatch) as harness:

        def read(index):
            entered[index].set()
            release.wait()
            try:
                return harness.app.state.session_service.get_session_for_stream(harness.session_id)
            finally:
                finished[index].set()

        async def frames(index):
            await run_stream_read_in_worker(read, index)
            yield b"data: released\n\n"

        try:
            for index in range(8):
                permit = permits.acquire(f"op-{index}", f"principal-{index // 4}")
                response = BoundedComposerStreamResponse(frames=frames(index), authorize=authorize, permits=permits, permit=permit)
                subscribers.append(asyncio.create_task(response({}, receive, send)))
            await _wait_for(lambda: all(item.is_set() for item in entered))
            assert outstanding_admissions() == 8
            assert permits.occupied == 8
            with pytest.raises(ComposerStreamCapacityError):
                permits.acquire("ninth", "third-principal")
            async with harness.client() as client:
                async with asyncio.timeout(30):
                    ordinary = await client.get(f"/api/sessions/{harness.session_id}")
                    assert ordinary.status_code == 200
                    operation_id = str(uuid4())
                    accepted = await client.post(
                        f"/api/sessions/{harness.session_id}/messages",
                        json={"content": "Ordinary request", "operation_id": operation_id, "state_id": None},
                    )
                    assert accepted.status_code == 202, accepted.text
                    snapshot = await settle(harness, operation_id=operation_id, client=client)
                # Integration completion point (U-40): the request completes and
                # publishes the one scripted reply through the shared pool.
                assert snapshot.status == "completed", snapshot.model_dump_json()
                assert assistant_contents(harness.app, harness.session_id) == ["Completed delayed response."]
            assert sdk.roles == ["primary", "end"]
            assert not any(item.is_set() for item in finished)
            assert outstanding_admissions() == 8
            await asyncio.gather(*subscribers)
            assert permits.occupied == 8
            assert outstanding_admissions() == 8
            release.set()
            await _wait_for(lambda: permits.occupied == 0 and outstanding_admissions() == 0)
            assert all(item.is_set() for item in finished)
        finally:
            release.set()
            await asyncio.gather(*subscribers, return_exceptions=True)
            await _wait_for(lambda: outstanding_admissions() == 0)


@pytest.mark.asyncio
async def test_shared_pool_sixteen_running_sixteen_queued_bounds_growth_and_actual_custody() -> None:
    release = Event()
    lock = Lock()
    started = 0
    completed = 0
    tasks = []
    assert outstanding_admissions() == 0
    executor = async_workers._get_shared_executor()
    generation = async_workers._generation_for(executor)
    assert executor._max_workers == 16
    assert async_workers._SHARED_EXECUTOR is executor
    assert async_workers._GENERATION_CUSTODIAN is generation

    def read():
        nonlocal started, completed
        with lock:
            started += 1
        release.wait()
        with lock:
            completed += 1
        return "actual-result"

    try:
        tasks = [asyncio.create_task(run_stream_read_in_worker(read)) for _ in range(32)]
        await _wait_for(lambda: started == 16 and outstanding_admissions() == 32)
        with pytest.raises(AsyncWorkerAdmissionTimeoutError):
            await run_stream_read_in_worker(read)
        assert started == 16 and completed == 0 and outstanding_admissions() == 32
        for task in tasks:
            task.cancel()
        await _wait_for(lambda: outstanding_admissions() == 16)
        assert started == 16 and completed == 0
        assert sum(task.done() for task in tasks) == 16
        for task in tasks:
            task.cancel()
        await asyncio.sleep(0.02)
        assert outstanding_admissions() == 16 and completed == 0
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert completed == 16 and started == 16
        assert outstanding_admissions() == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _wait_for(lambda: outstanding_admissions() == 0)
