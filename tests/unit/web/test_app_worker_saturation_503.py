"""A saturated shared worker pool answers 503 ``database_unavailable`` at the app boundary.

``AsyncWorkerAdmissionTimeoutError`` is a ``TimeoutError`` and therefore an
``OSError``. Starlette looks exception handlers up along the exception's MRO,
so without an exact-class handler the refusal reached ``OSError``'s storage
handler, which re-raises anything without a retryable errno, and the request
ended as a 500. These tests drive real saturation through ``create_app``:
``ADMISSION_CAPACITY`` real ``run_sync_in_worker`` submissions are held on a
blocking callable, and the route's own next worker call is refused by the
real admission wait.

The pool is filled from inside the request, at the route's first call to
``get_current_state``, so authentication, ownership and (for the two writer
routes) the COMPOSE lease have already succeeded: the 503 is the route's
answer, not a dependency's refusal.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretBytes
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.types import Message, Receive, Scope, Send
from structlog.testing import capture_logs

from elspeth.web import async_workers
from elspeth.web.app import create_app, lifespan
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.sessions.protocol import CompositionStateRecord
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

_REQUEST_ID = "pool-refusal-1"
_DATABASE_UNAVAILABLE = {
    "detail": "Database is currently unavailable. Please retry in a moment.",
    "error_type": "database_unavailable",
    "request_id": _REQUEST_ID,
}


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_boot_probe_enabled=False,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )


def _app(tmp_path: Path, cleanup: ExitStack) -> FastAPI:
    app = create_app(_settings(tmp_path), process_watchdog_factory=OwnedTestProcessWatchdog)
    cleanup.callback(app.state._auth_audit_finalizer)
    cleanup.callback(app.state._session_engine_finalizer)
    cleanup.callback(app.state.readiness_probe_runner.close)
    return app


async def _alice() -> UserIdentity:
    return UserIdentity(user_id="alice", username="alice")


@asynccontextmanager
async def _served_session(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, AsyncClient, str]]:
    """The full application with lifespan, and one session alice owns."""
    with ExitStack() as cleanup:
        app = _app(tmp_path, cleanup)
        with app.state.session_engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
            grant_test_pipeline_user(conn, identity_id="alice")
        app.dependency_overrides[get_current_user] = _alice
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with lifespan(app), AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/sessions", json={"title": "Saturated pool"})
            assert created.status_code == 201, created.text
            yield app, client, created.json()["id"]


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class _SaturateThenRead:
    """Fill the shared pool, then hand the call to the real ``get_current_state``.

    The real method's own ``run_sync_in_worker`` admission is what refuses.
    """

    def __init__(self, original: Callable[..., Awaitable[CompositionStateRecord | None]]) -> None:
        self._original = original
        self._release = threading.Event()
        self._held: list[asyncio.Task[bool]] = []
        self.calls = 0

    async def __call__(self, session_id: UUID, **kwargs: Any) -> CompositionStateRecord | None:
        self.calls += 1
        if not self._held:
            self._held.extend(
                asyncio.create_task(async_workers.run_sync_in_worker(self._release.wait, 30))
                for _ in range(async_workers.ADMISSION_CAPACITY)
            )
            await _wait_until(lambda: async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY)
        return await self._original(session_id, **kwargs)

    async def drain(self) -> None:
        # Unconditional: the pool is process-wide, so a leaked admission would
        # refuse every later worker-backed test in this process.
        self._release.set()
        await asyncio.wait_for(asyncio.gather(*self._held, return_exceptions=True), timeout=30)


_ROUTES = [
    pytest.param("GET", "/api/sessions/{session_id}/state/yaml", id="state-yaml"),
    pytest.param("POST", "/api/sessions/{session_id}/mark-ready-for-review", id="mark-ready-for-review"),
    pytest.param("GET", "/api/sessions/{session_id}/shareable-link", id="shareable-link"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _ROUTES)
async def test_a_saturated_worker_pool_answers_503_database_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    async with _served_session(tmp_path) as (app, client, session_id):
        service = app.state.session_service
        saturate = _SaturateThenRead(service.get_current_state)
        monkeypatch.setattr(service, "get_current_state", saturate)
        try:
            with capture_logs() as logs:
                response = await client.request(method, path.format(session_id=session_id), headers={"X-Request-ID": _REQUEST_ID})
        finally:
            await saturate.drain()

    # The route body ran: authentication, ownership and any lease succeeded,
    # and its first worker call met the full pool.
    assert saturate.calls == 1
    assert response.status_code == 503, response.text
    assert response.json() == _DATABASE_UNAVAILABLE
    # The refusal's own message names the pool; it reaches no client.
    assert "saturated" not in response.text
    events = [log for log in logs if log["event"] == "http_worker_admission_timeout"]
    assert len(events) == 1
    assert events[0]["exc_class"] == "AsyncWorkerAdmissionTimeoutError"
    assert events[0]["request_id"] == _REQUEST_ID
    assert [log for log in logs if log["event"] == "http_storage_unavailable"] == []
    assert async_workers.outstanding_admissions() == 0


def test_the_exact_class_handler_wins_the_mro_lookup_over_oserror(tmp_path: Path) -> None:
    with ExitStack() as cleanup:
        handlers = _app(tmp_path, cleanup).exception_handlers
    # Starlette's lookup: the first class in the exception's MRO with a
    # registered handler. OSError is registered and sits later in the MRO.
    assert OSError in AsyncWorkerAdmissionTimeoutError.__mro__
    assert OSError in handlers
    selected = next(handlers[cls] for cls in AsyncWorkerAdmissionTimeoutError.__mro__ if cls in handlers)
    assert selected is handlers[AsyncWorkerAdmissionTimeoutError]
    assert selected is not handlers[OSError]


@pytest.mark.asyncio
async def test_a_websocket_scope_keeps_the_original_propagating(tmp_path: Path) -> None:
    """Before this handler, a WebSocket endpoint's refusal re-raised from the
    OSError handler unchanged. It still does: no HTTP body is rendered on a
    WebSocket scope. Driven through Starlette's own exception middleware with
    the application's registered handlers."""
    with ExitStack() as cleanup:
        handlers = dict(_app(tmp_path, cleanup).exception_handlers)
    refusal = AsyncWorkerAdmissionTimeoutError("async worker pool saturated")

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        raise refusal

    async def receive() -> Message:
        raise AssertionError("nothing may read from the socket")

    async def send(message: Message) -> None:
        raise AssertionError("nothing may be sent on the socket")

    middleware = ExceptionMiddleware(endpoint, handlers=handlers)
    scope: Scope = {"type": "websocket", "path": "/ws/runs/r", "headers": [], "query_string": b""}
    with pytest.raises(AsyncWorkerAdmissionTimeoutError) as raised:
        await middleware(scope, receive, send)
    assert raised.value is refusal
