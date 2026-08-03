"""Composer request lifecycle telemetry integration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes.composer import guided


@dataclass
class _Registry:
    events: list[tuple[str, str]] = field(default_factory=list)

    def begin_request(self, session_id: str) -> None:
        self.events.append(("begin", session_id))

    def end_request(self, session_id: str) -> None:
        self.events.append(("end", session_id))


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


async def _settle_dependency(
    *,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | None = None,
) -> tuple[list[tuple[Any, ...]], _Registry]:
    registry = _Registry()
    lifecycle: list[tuple[Any, ...]] = []
    token = object()

    def begin_metrics(*, surface: str) -> object:
        lifecycle.append(("begin", surface))
        return token

    def finish_metrics(observed_token: object, *, status: str) -> None:
        lifecycle.append(("finish", observed_token, status))

    monkeypatch.setattr(_helpers, "_get_composer_progress_registry", lambda request: registry)
    monkeypatch.setattr(
        _helpers,
        "begin_composer_request_metrics",
        begin_metrics,
        raising=False,
    )
    monkeypatch.setattr(
        _helpers,
        "finish_composer_request_metrics",
        finish_metrics,
        raising=False,
    )
    dependency = cast("AsyncGenerator[None, None]", _helpers._track_compose_inflight(uuid4(), _request(path)))
    await anext(dependency)
    if failure is None:
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)
    else:
        with pytest.raises(type(failure)):
            await dependency.athrow(failure)
    return lifecycle, registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "surface"),
    (
        ("/api/sessions/1/messages", "freeform"),
        ("/api/sessions/1/guided/plan", "guided"),
        ("/api/sessions/1/guided/respond", "guided"),
        ("/api/sessions/1/guided/chat", "guided"),
    ),
)
async def test_request_dependency_projects_closed_surface_and_success(
    path: str,
    surface: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, registry = await _settle_dependency(path=path, monkeypatch=monkeypatch)

    assert len(lifecycle) == 2
    assert lifecycle[0] == ("begin", surface)
    assert lifecycle[1][0] == "finish"
    assert lifecycle[1][2] == "completed"
    assert [event for event, _session_id in registry.events] == ["begin", "end"]


def test_guided_respond_route_mounts_request_lifecycle_dependency_exactly_once() -> None:
    routes = [route for route in guided.router.routes if isinstance(route, APIRoute) and route.path == "/{session_id}/guided/respond"]

    assert len(routes) == 1
    dependencies = [dependency.call for dependency in routes[0].dependant.dependencies]
    assert dependencies.count(_helpers._track_compose_inflight) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    (
        (RuntimeError("provider failed"), "failed"),
        (TimeoutError("provider timed out"), "timed_out"),
        (HTTPException(status_code=504), "timed_out"),
        (HTTPException(status_code=499, detail="cancelled"), "cancelled"),
        (asyncio.CancelledError(), "cancelled"),
    ),
)
async def test_request_dependency_projects_closed_failure_status(
    failure: BaseException,
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, registry = await _settle_dependency(
        path="/api/sessions/1/guided/respond",
        monkeypatch=monkeypatch,
        failure=failure,
    )

    assert len(lifecycle) == 2
    assert lifecycle[0] == ("begin", "guided")
    assert lifecycle[1][0] == "finish"
    assert lifecycle[1][2] == status
    assert [event for event, _session_id in registry.events] == ["begin", "end"]


def test_existing_terminal_counter_also_marks_request_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    marked: list[str] = []
    counter_events: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(_helpers, "mark_composer_request_terminal", marked.append, raising=False)
    monkeypatch.setattr(
        _helpers._COMPOSER_REQUEST_TERMINAL_COUNTER,
        "add",
        lambda value, attributes: counter_events.append((value, attributes)),
    )

    _helpers._record_composer_request_terminal("timed_out", endpoint="send_message")

    assert marked == ["timed_out"]
    assert counter_events == [(1, {"endpoint": "send_message", "status": "timed_out"})]
