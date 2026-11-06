"""Signal-time latching precedes loop scheduling and HTTP shutdown."""

import asyncio
import signal as signals

import pytest

from elspeth.web.sessions.composer_drain import ComposerDrainDeadline, ComposerDrainSignal


def test_server_latches_draining_synchronously_before_uvicorn_exit() -> None:
    import uvicorn

    from elspeth.web.composer_server import ComposerServer

    server = ComposerServer(uvicorn.Config("elspeth.web.app:create_app", factory=True))
    latch = ComposerDrainSignal()
    server.drain_signal = latch
    server.handle_exit(signals.SIGTERM, None)
    assert latch.draining.is_set() and latch.started_at is not None
    assert server.should_exit


@pytest.mark.asyncio
async def test_signal_sets_the_shared_event_before_the_scheduled_callback() -> None:
    signal = ComposerDrainSignal()
    observations: list[float] = []

    def begin(started_at: float) -> ComposerDrainDeadline:
        observations.append(started_at)
        return ComposerDrainDeadline.from_start(started_at=started_at, drain_seconds=10.0)

    signal.bind(asyncio.get_running_loop(), begin)
    signal.request()
    started = signal.started_at
    assert started is not None and signal.draining.is_set()
    assert observations == []
    await asyncio.sleep(0)
    assert observations == [started]
    signal.request()
    await asyncio.sleep(0)
    assert observations == [started]


@pytest.mark.asyncio
async def test_signal_before_worker_binding_keeps_the_original_start() -> None:
    signal = ComposerDrainSignal()
    signal.request()
    started = signal.started_at
    observations: list[float] = []

    def begin(value: float) -> ComposerDrainDeadline:
        observations.append(value)
        return ComposerDrainDeadline.from_start(started_at=value, drain_seconds=10.0)

    signal.bind(asyncio.get_running_loop(), begin)
    assert observations == [started]
