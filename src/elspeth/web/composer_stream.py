"""Bounded ASGI delivery for authenticated Composer progress subscribers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError

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
    ) -> None:
        super().__init__(
            content=None,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        outstanding: set[asyncio.Task[object]] = set()
        headers_attempted = False
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

        async def emit(message: Message) -> None:
            if message["type"] == "http.response.body" and message["body"] and self._may_send is not None and not self._may_send():
                raise ComposerStreamAuthorizationDenied
            await send(message)

        disconnect = asyncio.create_task(disconnected())
        try:
            if not await bounded(self._authorize(), 2.0):
                raise HTTPException(status_code=404, detail="Session not found", headers={"Cache-Control": "no-store"})
            headers_attempted = True
            await bounded(
                emit({"type": "http.response.start", "status": 200, "headers": self.raw_headers}),
                self._send_deadline,
                cancel_on_timeout=True,
            )
            deadline = asyncio.get_running_loop().time() + self._lifetime
            iterator = self._frames.__aiter__()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or disconnect.done():
                    break

                async def read_frame() -> bytes:
                    return await anext(iterator)

                next_frame = asyncio.create_task(read_frame())
                outstanding.add(next_frame)
                next_frame.add_done_callback(_observe_task)
                read_deadline = min(deadline, asyncio.get_running_loop().time() + 2.0)
                while not next_frame.done():
                    done, _ = await asyncio.wait({next_frame, disconnect}, timeout=min(0.05, remaining))
                    if disconnect in done or asyncio.get_running_loop().time() >= read_deadline:
                        structlog.get_logger(__name__).info(
                            "composer_stream.closed",
                            reason="peer_disconnect" if disconnect in done else "read_deadline",
                            read_remaining_seconds=read_deadline - asyncio.get_running_loop().time(),
                        )
                        return
                    remaining = deadline - asyncio.get_running_loop().time()
                try:
                    frame = next_frame.result()
                except StopAsyncIteration:
                    break
                if disconnect.done() or asyncio.get_running_loop().time() >= deadline:
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
            await bounded(
                emit({"type": "http.response.body", "body": b"", "more_body": False}), self._send_deadline, cancel_on_timeout=True
            )
        except ComposerStreamAuthorizationDenied:
            structlog.get_logger(__name__).info("composer_stream.closed", reason="expiry_before_send")
            await bounded(
                send({"type": "http.response.body", "body": b"", "more_body": False}), self._send_deadline, cancel_on_timeout=True
            )
        except (TimeoutError, SQLAlchemyError, AuditIntegrityError, AsyncWorkerAdmissionTimeoutError) as exc:
            structlog.get_logger(__name__).info(
                "composer_stream.closed", reason="bounded_io_failure", failure_class=type(exc).__name__, headers_attempted=headers_attempted
            )
            if not headers_attempted:
                raise HTTPException(
                    status_code=503, detail="Composer stream is temporarily unavailable", headers={"Cache-Control": "no-store"}
                ) from exc
            # The connection closes; unfinished I/O retains subscriber capacity.
            return
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
