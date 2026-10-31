"""Composer-Operation-Version 2 SSE stream (U-41 step 3).

Design §3.4 (frames), §3.6 (Last-Event-ID, state-transfer resume), §3.7
(lifetime, drain). Each test names its mutation control.

The in-process ASGI transport returns a body only once the response ends, so
each route test runs the stream as a task and settles the seeded operation
partway through; the stream then sends ``terminal`` and closes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from starlette.types import Message
from structlog.testing import capture_logs
from tests.helpers.composer_continuation_rows import PRIVATE_MARKERS, insert_v2_operation, settle_v2_operation_failed
from tests.helpers.composer_operations import build_composer_operation_app

from elspeth.web.composer.progress import ComposerProgressSnapshot
from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamPermits
from elspeth.web.sessions.composer_operation_versions import COMPOSER_OPERATION_VERSION_HEADER


def _events(body: str) -> list[dict[str, object]]:
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, object] = {}
        for line in block.split("\n"):
            field, _, value = line.partition(": ")
            frame[field] = json.loads(value) if field == "data" else value
        frames.append(frame)
    return frames


async def _stream_until_settled(setup, seeded, *, settle_after: float, headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}", COMPOSER_OPERATION_VERSION_HEADER: "2", **(headers or {})},
        timeout=60,
    ) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/stream"))
        await asyncio.sleep(settle_after)
        settle_v2_operation_failed(
            setup.app.state.session_engine,
            session_id=setup.session_id,
            operation_id=seeded.operation_id,
            status_revision=seeded.status_revision + 1,
        )
        return await asyncio.wait_for(request, timeout=30)


def _seed(setup, state="running", **kwargs):
    return insert_v2_operation(
        setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="transport-user", state=state, **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("last_event_id", [None, "5"])
async def test_first_frame_is_status_keyed_by_revision_and_terminal_carries_no_result(tmp_path, last_event_id) -> None:
    """E-1 and E-3. Controls: skip the first status when Last-Event-ID equals the revision -> the "5" case is red;
    put the sealed result in the terminal frame -> red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, status_revision=5)
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    response = await _stream_until_settled(setup, seeded, settle_after=1.5, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers[COMPOSER_OPERATION_VERSION_HEADER] == "2"
    frames = _events(response.text)
    first, last = frames[0], frames[-1]
    assert (first["event"], first["id"]) == ("status", "5")
    assert first["data"]["schema_version"] == "composer-operation-stream.v2"
    assert first["data"]["payload"]["status_revision"] == 5
    assert set(first["data"]["payload"]) == {
        "protocol_version",
        "operation_id",
        "kind",
        "status",
        "phase",
        "status_revision",
        "created_at",
        "observed_at",
        "poll_after_ms",
        "cancel_requested",
        "segment",
    }
    assert (last["event"], last["id"]) == ("terminal", "6")
    assert last["data"]["payload"] == {"status": "failed", "phase": "settled", "status_revision": 6}
    assert [frame["data"]["sequence"] for frame in frames] == list(range(len(frames)))


@pytest.mark.asyncio
async def test_unchanged_revision_sends_ticks_and_heartbeats_not_status_and_stays_open(tmp_path) -> None:
    """E-2, through the complete ASGI response. Controls: send status every poll -> red (more than one status);
    stop yielding the empty tick -> red (the wrapper closes on its 2 s read deadline before the terminal)."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup)
    response = await _stream_until_settled(setup, seeded, settle_after=11.5)
    events = [frame["event"] for frame in _events(response.text)]
    # One status on connect; the next status is the settlement's own revision, just before terminal.
    assert events[0] == "status"
    assert events[-2:] == ["status", "terminal"]
    assert events[1:-2].count("status") == 0
    assert events.count("heartbeat") >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "99999999999999999999", ""])
async def test_malformed_last_event_id_is_refused_before_any_byte(tmp_path, value) -> None:
    """E-4. Control: ignore malformed values -> red (a stream opens)."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}", COMPOSER_OPERATION_VERSION_HEADER: "2", "Last-Event-ID": value},
    ) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/stream")
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["error_type"] == "composer_operation_resume_token_invalid"


@pytest.mark.asyncio
async def test_valid_last_event_id_is_logged_as_the_resume_point(tmp_path) -> None:
    """E-4 (log). Control: drop resumed_from_revision from the open line -> red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, status_revision=4)
    with capture_logs() as logs:
        await _stream_until_settled(setup, seeded, settle_after=1.2, headers={"Last-Event-ID": "4"})
    opened = [entry for entry in logs if entry["event"] == "composer_stream.opened"]
    assert opened == [{"event": "composer_stream.opened", "protocol_version": 2, "resumed_from_revision": 4, "log_level": "info"}]


class _BoundProgressRegistry:
    """Returns one progress snapshot bound to the seeded fence, for every lookup."""

    def __init__(self, operation_id: str, session_id: str) -> None:
        self.snapshot = ComposerProgressSnapshot(
            session_id=session_id,
            request_id="request-1",
            operation_id=operation_id,
            session_operation_id=PRIVATE_MARKERS["fence_id"],
            session_operation_epoch=7,
            request_token="PRIVATE-REQUEST-TOKEN-MARKER",
            updated_at=datetime.now(UTC),
            phase="calling_model",
            headline="Asking the model for the next step",
        )

    async def get_for_operation(self, **kwargs) -> ComposerProgressSnapshot | None:
        if (kwargs["session_operation_id"], kwargs["session_operation_epoch"]) != (PRIVATE_MARKERS["fence_id"], 7):
            return None
        return self.snapshot


@pytest.mark.asyncio
async def test_v2_progress_frame_is_segment_bound_and_carries_no_fence_or_token(tmp_path) -> None:
    """B-10 (stream). Control: reuse the v1 progress payload in v2 -> red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup)
    setup.app.state.composer_progress_registry = _BoundProgressRegistry(seeded.operation_id, str(setup.session_id))
    response = await _stream_until_settled(setup, seeded, settle_after=1.5)
    progress = [frame for frame in _events(response.text) if frame["event"] == "progress"]
    assert len(progress) == 1
    payload = progress[0]["data"]["payload"]
    assert payload["segment_sequence"] == 1 and payload["segment_generation"] == 1
    assert payload["headline"] == "Asking the model for the next step"
    assert not {"session_operation_id", "session_operation_epoch", "request_token"} & set(payload)
    for marker in (*PRIVATE_MARKERS.values(), "PRIVATE-REQUEST-TOKEN-MARKER", seeded.checkpoint_id):
        assert marker not in response.text


@pytest.mark.asyncio
async def test_draining_instance_sends_reconnect_and_ends(tmp_path) -> None:
    """Drain (§3.7). Control: remove the instance_draining check -> red (no reconnect; the stream runs on)."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}", COMPOSER_OPERATION_VERSION_HEADER: "2"},
    ) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/stream"))
        await asyncio.sleep(1.2)
        setup.app.state.instance_draining.set()
        response = await asyncio.wait_for(request, timeout=10)
    events = [frame["event"] for frame in _events(response.text)]
    assert events[0] == "status"
    assert events[-1] == "reconnect"
    assert "terminal" not in events


# --- wrapper-level -------------------------------------------------------


def _wrapper(frames, *, lifetime: float, lifetime_frame=None) -> BoundedComposerStreamResponse:
    permits = ComposerStreamPermits()
    permit = permits.acquire("operation", "principal")

    async def authorize() -> bool:
        return True

    return BoundedComposerStreamResponse(
        frames=frames, authorize=authorize, permits=permits, permit=permit, lifetime=lifetime, lifetime_frame=lifetime_frame
    )


async def _run(response: BoundedComposerStreamResponse) -> list[bytes]:
    bodies: list[bytes] = []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        # Every data chunk, empty ones included; only the closing chunk is excluded.
        if message["type"] == "http.response.body" and message.get("more_body", False):
            bodies.append(message["body"])

    await response({}, receive, send)
    return bodies


@pytest.mark.asyncio
async def test_ticks_keep_a_silent_stream_open_past_the_read_deadline() -> None:
    """E-2 (wrapper). Control: treat b"" as a frame to send -> red (an empty body chunk is emitted);
    a generator silent for 3 s without ticks closes on the 2 s read deadline (second case)."""

    async def with_ticks():
        for _ in range(6):
            await asyncio.sleep(0.5)
            yield b""
        yield b"event: heartbeat\ndata: {}\n\n"

    async def without_ticks():
        await asyncio.sleep(3)
        yield b"event: heartbeat\ndata: {}\n\n"

    assert await _run(_wrapper(with_ticks(), lifetime=30)) == [b"event: heartbeat\ndata: {}\n\n"]
    assert await _run(_wrapper(without_ticks(), lifetime=30)) == []


@pytest.mark.asyncio
async def test_lifetime_end_sends_the_reconnect_frame_once() -> None:
    """E-6. Control: drop the lifetime frame send -> red; a stream without one (v1) sends nothing extra."""

    async def ticks_forever():
        while True:
            await asyncio.sleep(0.05)
            yield b""

    calls = []

    def reconnect() -> bytes:
        calls.append(1)
        return b"event: reconnect\ndata: {}\n\n"

    assert await _run(_wrapper(ticks_forever(), lifetime=0.3, lifetime_frame=reconnect)) == [b"event: reconnect\ndata: {}\n\n"]
    assert calls == [1]
    assert await _run(_wrapper(ticks_forever(), lifetime=0.3)) == []


# --- review R-4, R-5, R-6 -------------------------------------------------


@pytest.mark.asyncio
async def test_connecting_to_an_already_terminal_v2_row_sends_status_then_terminal(tmp_path) -> None:
    """R-6. Control: check terminal before the first status -> red (terminal first)."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, state="failed", status_revision=9)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}", COMPOSER_OPERATION_VERSION_HEADER: "2"},
    ) as client:
        response = await asyncio.wait_for(
            client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/stream"), timeout=10
        )
    frames = _events(response.text)
    assert [(frame["event"], frame["id"]) for frame in frames] == [("status", "9"), ("terminal", "9")]
    assert frames[0]["data"]["payload"]["segment"] is None


@pytest.mark.asyncio
async def test_v1_stream_ignores_last_event_id_as_before(tmp_path) -> None:
    """R-4. Control: validate Last-Event-ID before lifecycle dispatch -> red (422 for a v1 row)."""
    from tests.helpers.composer_operations import message_body

    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(__import__("uuid").uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}"},
        timeout=30,
    ) as client:
        admitted = await client.post(f"/api/sessions/{setup.session_id}/messages", json=message_body("v1", operation_id=operation_id))
        assert admitted.status_code == 202
        request = asyncio.create_task(
            client.get(f"/api/sessions/{setup.session_id}/operations/{operation_id}/stream", headers={"Last-Event-ID": "abc"})
        )
        await asyncio.sleep(1.2)
        stopped = await client.post(f"/api/sessions/{setup.session_id}/operations/{operation_id}/cancel")
        assert stopped.status_code == 200
        response = await asyncio.wait_for(request, timeout=10)
    assert response.status_code == 200, response.text
    assert response.headers[COMPOSER_OPERATION_VERSION_HEADER] == "1"
    events = [frame["event"] for frame in _events(response.text)]
    assert events[-1] == "terminal" and "reconnect" not in events
    assert all(frame["data"]["schema_version"] == "composer-operation-stream.v1" for frame in _events(response.text))


@pytest.mark.asyncio
async def test_drain_between_authorization_reads_still_sends_reconnect() -> None:
    """R-5: the shared pool refuses reads once draining. Control: drop the drain branch in the
    bounded-failure handler -> red (the stream closes without reconnect)."""
    import threading

    from elspeth.web.required_executor import RequiredGenerationUnavailable

    draining = threading.Event()
    permits = ComposerStreamPermits()
    permit = permits.acquire("operation", "principal")
    calls = []

    async def authorize() -> bool:
        calls.append(1)
        if len(calls) == 1:
            return True
        draining.set()
        raise RequiredGenerationUnavailable("Shared executor lifecycle unavailable")

    async def frames():
        yield b"event: status\ndata: {}\n\n"

    response = BoundedComposerStreamResponse(
        frames=frames(),
        authorize=authorize,
        permits=permits,
        permit=permit,
        lifetime=30,
        lifetime_frame=lambda: b"event: reconnect\ndata: {}\n\n",
        draining=draining,
    )
    assert await _run(response) == [b"event: reconnect\ndata: {}\n\n"]


@pytest.mark.asyncio
async def test_drain_after_authorization_expiry_closes_without_the_reconnect_frame() -> None:
    """The drain-time reconnect goes through the expiry gate like every body chunk. Expired
    authorization withholds the frame and the response still closes. Control: drop the expiry
    handling around the close-path reconnect -> red (ComposerStreamAuthorizationDenied escapes)."""
    import threading

    from elspeth.web.required_executor import RequiredGenerationUnavailable

    draining = threading.Event()
    permits = ComposerStreamPermits()
    permit = permits.acquire("operation", "principal")
    calls = []
    messages: list[Message] = []

    async def authorize() -> bool:
        calls.append(1)
        if len(calls) == 1:
            return True
        draining.set()
        raise RequiredGenerationUnavailable("Shared executor lifecycle unavailable")

    async def frames():
        yield b"event: status\ndata: {}\n\n"

    async def receive():
        await asyncio.sleep(3600)

    async def send(message: Message) -> None:
        messages.append(message)

    response = BoundedComposerStreamResponse(
        frames=frames(),
        authorize=authorize,
        permits=permits,
        permit=permit,
        may_send=lambda: not draining.is_set(),
        lifetime=30,
        lifetime_frame=lambda: b"event: reconnect\ndata: {}\n\n",
        draining=draining,
    )
    await response({}, receive, send)
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    for _ in range(5):
        await asyncio.sleep(0)
    assert permits.occupied == 0


@pytest.mark.asyncio
async def test_drain_ends_a_stream_whose_reads_still_succeed() -> None:
    """The wrapper's own drain check, independent of the pool refusing reads.
    Control: ignore the drain event in the read loop -> red (the stream runs to its lifetime)."""
    import threading

    draining = threading.Event()
    permits = ComposerStreamPermits()
    permit = permits.acquire("operation", "principal")

    async def authorize() -> bool:
        return True

    async def ticks():
        while True:
            await asyncio.sleep(0.05)
            yield b""

    async def drain_soon() -> None:
        await asyncio.sleep(0.3)
        draining.set()

    response = BoundedComposerStreamResponse(
        frames=ticks(),
        authorize=authorize,
        permits=permits,
        permit=permit,
        lifetime=20,
        lifetime_frame=lambda: b"event: reconnect\ndata: {}\n\n",
        draining=draining,
    )
    setter = asyncio.create_task(drain_soon())
    bodies = await asyncio.wait_for(_run(response), timeout=5)
    await setter
    assert bodies == [b"event: reconnect\ndata: {}\n\n"]
