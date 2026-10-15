from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Message

from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamCapacityError, ComposerStreamPermits


def test_subscriber_caps_operation_principal_and_instance() -> None:
    permits = ComposerStreamPermits()
    first = permits.acquire("one", "alice")
    permits.acquire("one", "alice")
    with pytest.raises(ComposerStreamCapacityError):
        permits.acquire("one", "bob")
    permits.acquire("two", "alice")
    permits.acquire("two", "alice")
    with pytest.raises(ComposerStreamCapacityError):
        permits.acquire("three", "alice")
    permits.release(first)
    permits.acquire("three", "alice")
    for index in range(4):
        permits.acquire(f"operation-{index}", f"principal-{index}")
    assert permits.occupied == 8
    with pytest.raises(ComposerStreamCapacityError):
        permits.acquire("extra", "extra")


@pytest.mark.asyncio
async def test_cancellation_resistant_send_retains_capacity_until_actual_completion() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "person")
    finish = asyncio.Event()
    started = asyncio.Event()

    async def frames():
        yield b"data: progress\n\n"

    async def authorize() -> bool:
        return True

    async def send(message):
        if message["type"] == "http.response.body":
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit, send_deadline=0.01)
    await response({}, receive, send)
    assert started.is_set()
    assert permits.occupied == 1
    finish.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
async def test_revocation_during_idle_wait_prevents_data_send() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "person")
    finish = asyncio.Event()
    checks = 0
    messages = []

    async def frames():
        await asyncio.sleep(0.02)
        yield b"data: private\n\n"

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)
    await response({}, receive, send)
    assert [message["body"] for message in messages if message["type"] == "http.response.body"] == [b""]
    finish.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["authorization", "read"])
async def test_subscriber_cancellation_retains_permit_until_owned_read_or_auth_finishes(blocked_phase: str) -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "person")
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def frames():
        if blocked_phase == "read":
            entered.set()
            await release.wait()
        yield b"data: owned\n\n"

    async def authorize() -> bool:
        if blocked_phase == "authorization":
            entered.set()
            await release.wait()
        return True

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)
    subscriber = asyncio.create_task(response({}, receive, send))
    await entered.wait()
    subscriber.cancel()
    with pytest.raises(asyncio.CancelledError):
        await subscriber
    assert permits.occupied == 1
    assert not any(message["type"] == "http.response.body" and message["body"] for message in sent)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0
    assert not any(message["type"] == "http.response.body" and message["body"] for message in sent)


@pytest.mark.asyncio
async def test_default_asgi_send_deadline_bounds_actual_send_and_keeps_resistant_permit() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    release = asyncio.Event()
    body_invocations = []

    async def frames():
        yield b"data: first\n\n"
        yield b"data: forbidden-second\n\n"

    async def authorize():
        return True

    async def send(message):
        if message["type"] == "http.response.body" and message["body"]:
            body_invocations.append(message["body"])
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    async def receive():
        await asyncio.Event().wait()

    start = asyncio.get_running_loop().time()
    await BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)({}, receive, send)
    elapsed = asyncio.get_running_loop().time() - start
    assert 4.95 <= elapsed < 8.0
    assert body_invocations == [b"data: first\n\n"]
    assert permits.occupied == 1
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["authorization", "read"])
async def test_actual_default_read_auth_deadline_retains_pending_custody(phase: str) -> None:
    from starlette.exceptions import HTTPException

    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    release = asyncio.Event()
    messages = []

    async def authorize():
        if phase == "authorization":
            await release.wait()
        return True

    async def frames():
        await release.wait()
        yield b"data: too-late\n\n"

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)
    start = asyncio.get_running_loop().time()
    if phase == "authorization":
        with pytest.raises(HTTPException) as caught:
            await response({}, receive, send)
        assert caught.value.status_code == 503
        assert messages == []
    else:
        await response({}, receive, send)
        assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
        assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    elapsed = asyncio.get_running_loop().time() - start
    assert 1.95 <= elapsed < 5.0
    assert permits.occupied == 1
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert permits.occupied == 0
    assert not any(message["type"] == "http.response.body" and message["body"] for message in messages)


@pytest.mark.asyncio
async def test_expiry_at_actual_frame_invocation_prevents_new_data_send() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    messages = []

    async def authorize():
        return True

    async def frames():
        yield b"data: expired\n\n"

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit, may_send=lambda: False)
    await response({}, receive, send)
    assert [message["body"] for message in messages if message["type"] == "http.response.body"] == [b""]
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["read", "authorization", "send"])
async def test_writable_stream_closes_after_bounded_io_failure(failure_phase: str) -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    messages: list[Message] = []
    checks = 0

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        if failure_phase == "authorization" and checks > 1:
            raise SQLAlchemyError("synthetic unavailable read")
        return True

    async def frames():
        if failure_phase == "read":
            raise SQLAlchemyError("synthetic unavailable read")
        yield b"data: progress\n\n"

    async def send(message: Message) -> None:
        if failure_phase == "send" and message["type"] == "http.response.body" and message["body"]:
            raise TimeoutError
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    await BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)({}, receive, send)
    assert messages == [
        {"type": "http.response.start", "status": 200, "headers": messages[0]["headers"]},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["headers", "frame", "final"])
async def test_timed_out_resistant_send_never_overlaps_or_retries(blocked_phase: str) -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    release = asyncio.Event()
    messages: list[Message] = []
    active = False

    async def authorize() -> bool:
        return True

    async def frames():
        yield b"data: progress\n\n"

    async def send(message: Message) -> None:
        nonlocal active
        assert not active, "ASGI sends must never overlap"
        active = True
        messages.append(message)
        phase = "headers" if message["type"] == "http.response.start" else "frame" if message["body"] else "final"
        try:
            if phase == blocked_phase:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
        finally:
            active = False

    async def receive():
        await asyncio.Event().wait()

    await BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit, send_deadline=0.01)(
        {}, receive, send
    )
    assert active
    assert permits.occupied == 1
    assert len(messages) == {"headers": 1, "frame": 2, "final": 3}[blocked_phase]
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not active
    assert permits.occupied == 0
    assert len(messages) == {"headers": 1, "frame": 2, "final": 3}[blocked_phase]


@pytest.mark.asyncio
async def test_peer_disconnect_during_read_does_not_attempt_final_body() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    release = asyncio.Event()
    reading = asyncio.Event()
    messages: list[Message] = []

    async def authorize() -> bool:
        return True

    async def frames():
        reading.set()
        await release.wait()
        yield b"data: private\n\n"

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        await reading.wait()
        return {"type": "http.disconnect"}

    await BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)({}, receive, send)
    assert [message["type"] for message in messages] == ["http.response.start"]
    assert permits.occupied == 1
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert permits.occupied == 0
    assert [message["type"] for message in messages] == ["http.response.start"]


@pytest.mark.asyncio
@pytest.mark.parametrize("final_failure", [False, True])
@pytest.mark.parametrize("ending", ["normal", "revocation", "expiry", "read_failure"])
async def test_stream_attempts_final_body_exactly_once(final_failure: bool, ending: str) -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    messages: list[Message] = []
    checks = 0

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        return ending != "revocation" or checks == 1

    async def frames():
        if ending == "read_failure":
            raise SQLAlchemyError("synthetic unavailable read")
        yield b"data: progress\n\n"

    async def send(message: Message) -> None:
        messages.append(message)
        if final_failure and message["type"] == "http.response.body" and not message["more_body"]:
            raise TimeoutError

    async def receive():
        await asyncio.Event().wait()

    response = BoundedComposerStreamResponse(
        frames=frames(), authorize=authorize, permits=permits, permit=permit, may_send=lambda: ending != "expiry"
    )
    try:
        await response({}, receive, send)
    except TimeoutError:
        pytest.fail("A failed closing send must remain a bounded transport failure")
    assert [message["type"] for message in messages] == ["http.response.start"] + ["http.response.body"] * (2 if ending == "normal" else 1)
    if ending == "normal":
        assert messages[1] == {"type": "http.response.body", "body": b"data: progress\n\n", "more_body": True}
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
async def test_unknown_frame_error_propagates_without_claiming_stream_completion() -> None:
    permits = ComposerStreamPermits()
    permit = permits.acquire("op", "alice")
    messages: list[Message] = []

    async def authorize() -> bool:
        return True

    async def frames():
        yield b"data: progress\n\n"
        raise ValueError("synthetic programming error")

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ValueError, match="synthetic programming error"):
        await BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit)({}, receive, send)
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    assert messages[-1]["more_body"] is True
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0
