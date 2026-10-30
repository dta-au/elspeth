"""Bounded ASGI delivery for authenticated Composer progress subscribers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from elspeth.web.required_executor import RequiredGenerationUnavailable

SEND_DEADLINE_SECONDS = 5.0
FRAME_LIMIT_BYTES = 64 * 1024
STREAM_LIFETIME_SECONDS = 120.0


class ComposerStreamAuthorizationDenied(Exception):
    """Expiry or authorization prevents a new frame invocation."""


class ComposerStreamCapacityError(Exception):
    """Subscriber capacity is occupied by active or unfinished I/O."""


@dataclass(frozen=True, slots=True)
class ComposerStreamPermit:
    operation_id: str
    principal_id: str


class ComposerStreamPermits:
    """Capacity follows actual I/O completion, including resistant cancellation."""

    def __init__(self) -> None:
        self._counts: dict[ComposerStreamPermit, int] = {}

    def acquire(self, operation_id: str, principal_id: str) -> ComposerStreamPermit:
        permit = ComposerStreamPermit(operation_id, principal_id)
        operation_count = sum(count for key, count in self._counts.items() if key.operation_id == operation_id)
        principal_count = sum(count for key, count in self._counts.items() if key.principal_id == principal_id)
        if operation_count >= 2 or principal_count >= 4 or sum(self._counts.values()) >= 8:
            raise ComposerStreamCapacityError
        self._counts[permit] = self._counts.get(permit, 0) + 1
        return permit

    def release(self, permit: ComposerStreamPermit) -> None:
        count = self._counts[permit]
        if count == 1:
            del self._counts[permit]
        else:
            self._counts[permit] = count - 1

    @property
    def occupied(self) -> int:
        return sum(self._counts.values())


def _observe_task(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


class BoundedComposerStreamResponse(Response):
    """Bound each real send; a timed-out send can never initiate another frame.

    A peer may finish a frame whose send started before authorization was lost.
    Every subsequent send requires a fresh authorization decision.
    """

    def __init__(
        self,
        *,
        frames: AsyncIterator[bytes],
        authorize: Callable[[], Awaitable[bool]],
        permits: ComposerStreamPermits,
        permit: ComposerStreamPermit,
        send_deadline: float = SEND_DEADLINE_SECONDS,
        lifetime: float = STREAM_LIFETIME_SECONDS,
        may_send: Callable[[], bool] | None = None,
        lifetime_frame: Callable[[], bytes | None] | None = None,
        extra_headers: dict[str, str] | None = None,
        draining: threading.Event | None = None,
    ) -> None:
        super().__init__(
            content=None,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                **(extra_headers or {}),
            },
        )
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self._frames = frames
        self._authorize = authorize
        self._permits = permits
        self._permit = permit
        self._send_deadline = send_deadline
        self._lifetime = lifetime
        self._may_send = may_send
        # Sent once, after authorization, when the lifetime ends a live stream.
        # A generator may also yield b"" (a tick): a completed bounded read with
        # nothing to send, which needs no authorization and sends no bytes.
        self._lifetime_frame = lifetime_frame
        # Once the instance drains, the shared pool refuses new reads, so the
        # stream ends at once: the reconnect frame (if any) carries no state and
        # is sent without a fresh authorization read; expiry still applies.
        self._draining = draining

    async def _send_lifetime_frame(
        self,
        bounded: Callable[..., Awaitable[object]],
        emit: Callable[[Message], Awaitable[None]],
    ) -> None:
        build = self._lifetime_frame
        self._lifetime_frame = None
        if build is None:
            return
        frame = build()
        if frame is None:
            return
        await bounded(emit({"type": "http.response.body", "body": frame, "more_body": True}), self._send_deadline, cancel_on_timeout=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        outstanding: set[asyncio.Task[object]] = set()
        headers_attempted = False
        headers_completed = False
        send_active = False
        final_attempted = False
        released = False
        cancellable: set[asyncio.Task[object]] = set()

        def release_if_finished(_task: asyncio.Task[object] | None = None) -> None:
            nonlocal released
            if not released and all(task.done() for task in outstanding):
                released = True
                self._permits.release(self._permit)

        async def bounded[T](operation: Awaitable[T], timeout: float, *, cancel_on_timeout: bool = False) -> T:
            async def perform() -> T:
                return await operation

            task = asyncio.create_task(perform())
            outstanding.add(task)
            if cancel_on_timeout:
                cancellable.add(task)
            task.add_done_callback(_observe_task)
            operation_deadline = asyncio.get_running_loop().time() + timeout
            while not task.done():
                operation_remaining = operation_deadline - asyncio.get_running_loop().time()
                if operation_remaining <= 0:
                    if cancel_on_timeout:
                        task.cancel()
                    raise TimeoutError
                await asyncio.wait({task}, timeout=min(0.05, operation_remaining))
            return task.result()

        async def disconnected() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return

        def expired() -> bool:
            return self._may_send is not None and not self._may_send()

        async def transmit(message: Message) -> None:
            nonlocal send_active
            send_active = True
            try:
                await send(message)
            finally:
                send_active = False

        async def emit(message: Message) -> None:
            if message["type"] == "http.response.body" and message["body"] and expired():
                raise ComposerStreamAuthorizationDenied
            await transmit(message)

        async def emit_unless_expired(message: Message) -> None:
            # At the close nothing remains to abort: expiry withholds the frame, not the close.
            if expired():
                structlog.get_logger(__name__).info("composer_stream.closed", reason="expiry_before_send")
                return
            await transmit(message)

        disconnect = asyncio.create_task(disconnected())

        async def finish_response(*, reconnect: bool = False) -> None:
            nonlocal final_attempted
            if not headers_completed or final_attempted or send_active or disconnect.done():
                return
            # A failed or resistant closing send is never retried. It remains
            # in outstanding so subscriber capacity follows actual completion.
            final_attempted = True
            try:
                if reconnect:
                    # Part of the close: the reconnect frame carries no state,
                    # and its send is bounded like the closing chunk after it.
                    await self._send_lifetime_frame(bounded, emit_unless_expired)
                await bounded(
                    emit({"type": "http.response.body", "body": b"", "more_body": False}), self._send_deadline, cancel_on_timeout=True
                )
            except TimeoutError:
                # The client did not take a bounded closing send. The response
                # is over and the durable state stays in SQL for the next read.
                structlog.get_logger(__name__).info("composer_stream.closed", reason="bounded_close_failure")

        try:
            if not await bounded(self._authorize(), 2.0):
                raise HTTPException(status_code=404, detail="Session not found", headers={"Cache-Control": "no-store"})
            headers_attempted = True
            await bounded(
                emit({"type": "http.response.start", "status": 200, "headers": self.raw_headers}),
                self._send_deadline,
                cancel_on_timeout=True,
            )
            headers_completed = True
            deadline = asyncio.get_running_loop().time() + self._lifetime
            iterator = self._frames.__aiter__()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                draining = self._draining is not None and self._draining.is_set()
                if (remaining <= 0 or draining) and not disconnect.done() and self._lifetime_frame is not None:
                    await self._send_lifetime_frame(bounded, emit)
                if remaining <= 0 or draining or disconnect.done():
                    if draining:
                        structlog.get_logger(__name__).info("composer_stream.closed", reason="draining")
                    break

                async def read_frame() -> bytes:
                    return await anext(iterator)

                next_frame = asyncio.create_task(read_frame())
                outstanding.add(next_frame)
                next_frame.add_done_callback(_observe_task)
                read_deadline = min(deadline, asyncio.get_running_loop().time() + 2.0)
                while not next_frame.done():
                    done, _ = await asyncio.wait({next_frame, disconnect}, timeout=min(0.05, remaining))
                    instance_draining = self._draining is not None and self._draining.is_set()
                    if disconnect in done or instance_draining or asyncio.get_running_loop().time() >= read_deadline:
                        lifetime_ended = disconnect not in done and (instance_draining or asyncio.get_running_loop().time() >= deadline)
                        structlog.get_logger(__name__).info(
                            "composer_stream.closed",
                            reason="peer_disconnect"
                            if disconnect in done
                            else "draining"
                            if instance_draining
                            else "lifetime"
                            if lifetime_ended
                            else "read_deadline",
                            read_remaining_seconds=read_deadline - asyncio.get_running_loop().time(),
                        )
                        if lifetime_ended and self._lifetime_frame is not None:
                            await self._send_lifetime_frame(bounded, emit)
                        await finish_response()
                        return
                    remaining = deadline - asyncio.get_running_loop().time()
                try:
                    frame = next_frame.result()
                except StopAsyncIteration:
                    break
                if disconnect.done():
                    break
                if frame == b"":
                    continue
                if asyncio.get_running_loop().time() >= deadline:
                    if self._lifetime_frame is not None:
                        await self._send_lifetime_frame(bounded, emit)
                    break
                if len(frame) > FRAME_LIMIT_BYTES:
                    raise ValueError("Composer progress frame exceeds the delivery bound")
                check_remaining = read_deadline - asyncio.get_running_loop().time()
                if check_remaining <= 0:
                    raise TimeoutError
                if not await bounded(self._authorize(), check_remaining):
                    structlog.get_logger(__name__).info("composer_stream.closed", reason="authorization_denied")
                    break
                await bounded(
                    emit({"type": "http.response.body", "body": frame, "more_body": True}), self._send_deadline, cancel_on_timeout=True
                )
            await finish_response()
        except ComposerStreamAuthorizationDenied:
            structlog.get_logger(__name__).info("composer_stream.closed", reason="expiry_before_send")
            await finish_response()
        # Unavailable or slow I/O only. AuditIntegrityError (our own rows
        # contradicting themselves) is not caught: it propagates as a crash.
        except (TimeoutError, SQLAlchemyError, RequiredGenerationUnavailable) as exc:
            structlog.get_logger(__name__).info(
                "composer_stream.closed", reason="bounded_io_failure", failure_class=type(exc).__name__, headers_attempted=headers_attempted
            )
            if not headers_attempted:
                raise HTTPException(
                    status_code=503, detail="Composer stream is temporarily unavailable", headers={"Cache-Control": "no-store"}
                ) from exc
            # Finish a writable response without overlapping a resistant send.
            # Unfinished reads and sends retain subscriber capacity separately.
            # A drain that began between authorization reads makes the shared
            # pool refuse them; the client should still reconnect at once.
            await finish_response(reconnect=self._draining is not None and self._draining.is_set())
        finally:
            disconnect.cancel()
            outstanding.add(disconnect)
            cancellable.add(disconnect)
            disconnect.add_done_callback(_observe_task)
            for task in outstanding:
                if not task.done():
                    if task in cancellable:
                        task.cancel()
                    task.add_done_callback(release_if_finished)
            release_if_finished()
