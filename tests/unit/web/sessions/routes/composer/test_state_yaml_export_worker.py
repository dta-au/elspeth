"""``GET /state/yaml`` records its export audit row through the shared worker pool.

The route's fenced ``mutate`` call is handed to ``run_sync_in_worker``. These
tests pin three properties of that hand-off: the call runs on a worker thread
while the event loop keeps serving other tasks; a cancelled request surfaces
``CancelledError`` to its caller while the worker call runs to completion; and
a draining instance refuses the call with ``RequiredGenerationUnavailable``,
the exception the earlier ``get_current_state`` offload raises in drain.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.helpers.worker_probe import WorkerProbe, run_with_probe
from tests.unit.web.sessions.test_routes_session_completed_telemetry import (
    _csv_state,
    _make_app_with_telemetry,
    _ready_readiness,
    _save_composition_state,
)

from elspeth.web import async_workers
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.models import composer_completion_events_table
from elspeth.web.sessions.telemetry import observed_value

_PREFLIGHT_TARGET = "elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state"


async def _pass_preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
    return ValidationResult(is_valid=True, checks=[], errors=[], readiness=_ready_readiness())


async def _seeded_app(tmp_path: Path) -> tuple[Any, Any, Any]:
    app, service = _make_app_with_telemetry(tmp_path)
    session = await service.create_session("alice", "Pipeline", "local")
    await _save_composition_state(service, session.id, _csv_state(), provenance="session_seed")
    return app, service, session


def _export_rows(app: Any) -> list[Any]:
    with app.state.session_engine.connect() as conn:
        return list(conn.execute(select(composer_completion_events_table)).all())


@pytest.mark.asyncio
async def test_yaml_export_audit_write_runs_in_a_worker_while_the_loop_runs(tmp_path: Path) -> None:
    app, service, session = await _seeded_app(tmp_path)
    authority_type = type(service.session_operation_authority)
    probe = WorkerProbe()
    loop_thread = threading.get_ident()

    with (
        patch(_PREFLIGHT_TARGET, side_effect=_pass_preflight),
        patch.object(authority_type, "mutate", probe.wrap(authority_type.mutate)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await run_with_probe(probe, client.get(f"/api/sessions/{session.id}/state/yaml"))

    assert probe.entered.is_set()
    # True only if a loop task released the call while it was running.
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    assert response.status_code == 200, response.text
    assert [row.event_type for row in _export_rows(app)] == ["export_yaml"]
    assert observed_value(app.state.sessions_telemetry.session_completed_total) == 1


@pytest.mark.asyncio
async def test_cancelled_yaml_export_raises_cancelled_error_and_the_worker_call_completes(tmp_path: Path) -> None:
    """The caller sees ``CancelledError``; the running worker call is not interrupted.

    Its outcome is discarded, so the route records no completion telemetry.
    """
    app, service, session = await _seeded_app(tmp_path)
    authority_type = type(service.session_operation_authority)
    probe = WorkerProbe()

    with (
        patch(_PREFLIGHT_TARGET, side_effect=_pass_preflight),
        patch.object(authority_type, "mutate", probe.wrap(authority_type.mutate)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request = asyncio.ensure_future(client.get(f"/api/sessions/{session.id}/state/yaml"))
            try:
                await probe.wait_entered()
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            finally:
                probe.released.set()
            await probe.wait_finished()

    assert probe.outcomes == [True]
    assert observed_value(app.state.sessions_telemetry.session_completed_total) == 0


@pytest.mark.asyncio
async def test_yaml_export_audit_write_is_refused_while_the_instance_drains(tmp_path: Path) -> None:
    """Drain begins after the earlier offloads; the audit write is refused, not run."""
    app, _service, session = await _seeded_app(tmp_path)

    async def drain_after_preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
        async_workers._INSTANCE_DRAINING.set()
        return await _pass_preflight()

    with patch(_PREFLIGHT_TARGET, side_effect=drain_after_preflight):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with pytest.raises(RequiredGenerationUnavailable):
                await client.get(f"/api/sessions/{session.id}/state/yaml")

    assert _export_rows(app) == []
    assert observed_value(app.state.sessions_telemetry.session_completed_total) == 0
