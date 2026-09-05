# tests/unit/web/sessions/routes/composer/test_state_review_debt_offload.py
"""elspeth-e5a38115a6 — the review-debt check runs off the event loop and
under a bound on both state-import routes.

Before the fix the seed and YAML-import handlers called the CPU-bound
``unsurfaceable_pending_interpretation_review_sites`` inline, under the
session's COMPOSE lease and compose lock. Measured (lane elspeth-6e,
2026-09-05): an 80-node seed held the loop for 49 s, a concurrent
``POST /api/sessions`` waited 47.6 s, and the seed outlived its 30 s lease.
Commit 1 of the ticket made the check linear; this pin covers the other
half — that however long the check takes, the loop stays free and the
route refuses at the configured bound instead of holding the lease open.

The oracle for the concurrency pin is a clock started BEFORE the test yields
to the seed task. A check that blocks the loop lets the seed run to
completion during that first yield, after which a late-started clock would
time a fast concurrent request and pass; the early clock times the whole
stall. Both routes share ``_review_debt_sites_off_loop``, so the seed route
(the one reachable without a source blob) stands for both.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.web.sessions.test_e2e_state_seed_route import _make_app, _ready_readiness, _valid_state

from elspeth.web.execution.schemas import ValidationResult

_CHECK_TARGET = "elspeth.web.composer.service.unsurfaceable_pending_interpretation_review_sites"
_PREFLIGHT_TARGET = "elspeth.web.sessions.routes._helpers._runtime_preflight_for_state"


async def _pass_preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
    return ValidationResult(is_valid=True, checks=[], errors=[], readiness=_ready_readiness())


def _blocking_check(seconds: float, entered: threading.Event) -> Any:
    def check(_state: Any) -> tuple[()]:
        entered.set()
        time.sleep(seconds)
        return ()

    return check


async def _longest_loop_stall(stop: asyncio.Event, *, tick: float = 0.05) -> float:
    """Return the longest time the event loop went without serving this ticker.

    The mutation oracle. A check called inline holds the loop for its whole
    duration, so the ticker's next turn arrives that late; a check on the
    worker pool leaves the ticker's turns on time. Unlike a concurrent HTTP
    request this reads nothing from the test database, so the measurement
    cannot be pre-empted by the single-connection harness.
    """
    worst = 0.0
    while not stop.is_set():
        before = perf_counter()
        await asyncio.sleep(tick)
        worst = max(worst, perf_counter() - before - tick)
    return worst


@pytest.mark.asyncio
async def test_seed_review_debt_check_does_not_stall_a_concurrent_request(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path, e2e_state_seed_enabled=True)
    session = await service.create_session("alice", "Seed", "local")
    seeded = _valid_state(tmp_path, session_id=str(session.id))
    entered = threading.Event()
    stop = asyncio.Event()

    with (
        patch(_CHECK_TARGET, new=_blocking_check(2.0, entered)),
        patch(_PREFLIGHT_TARGET, side_effect=_pass_preflight),
    ):
        # raise_app_exceptions=False: under a loop-blocking mutant the
        # single-connection test database can fault the concurrent request;
        # that must surface as a failed assertion below, after the stall
        # measurement, not as an exception that skips it.
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            monitor = asyncio.create_task(_longest_loop_stall(stop))
            started = perf_counter()  # BEFORE the first yield to the seed task
            seed = asyncio.create_task(client.post(f"/api/sessions/{session.id}/state/e2e-seed", json={"state": seeded.to_dict()}))
            await asyncio.sleep(0.2)
            # The ticket's stated bar: a cheap unrelated request during the
            # seed stays sub-second. Timed from BEFORE the first yield, so a
            # loop-blocking check cannot run to completion unobserved.
            concurrent = await client.post("/api/sessions", json={"title": "concurrent"})
            concurrent_done_after = perf_counter() - started
            seed_response = await seed
            stop.set()
            longest_stall = await monitor

    assert entered.is_set()  # the seed really was inside the 2 s check
    assert longest_stall < 0.5, longest_stall  # inline call: >= 2.0 s
    assert concurrent.status_code in (200, 201), concurrent.text
    assert concurrent_done_after < 1.0, concurrent_done_after
    assert seed_response.status_code == 200, seed_response.text


@pytest.mark.asyncio
async def test_seed_review_debt_check_refuses_at_the_configured_bound(tmp_path: Path) -> None:
    """The bound is the runtime-preflight knob; the refusal is static and the
    state is not persisted — the lease is released instead of outlived."""
    app, service = _make_app(tmp_path, e2e_state_seed_enabled=True)
    app.state.settings = app.state.settings.model_copy(update={"composer_runtime_preflight_timeout_seconds": 0.2})
    session = await service.create_session("alice", "Seed", "local")
    seeded = _valid_state(tmp_path, session_id=str(session.id))
    entered = threading.Event()

    with (
        patch(_CHECK_TARGET, new=_blocking_check(1.0, entered)),
        patch(_PREFLIGHT_TARGET, side_effect=_pass_preflight),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/sessions/{session.id}/state/e2e-seed", json={"state": seeded.to_dict()})

    assert entered.is_set()
    assert response.status_code == 504, response.text
    assert response.json()["detail"] == "Review-debt check did not complete within the configured bound; seed aborted."
    assert await service.get_current_state(session.id) is None
