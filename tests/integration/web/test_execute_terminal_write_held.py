"""A slow terminal status write must not turn a completed run into a failed one.

Through the real routes, execution service, engine and session store: the
run's last status write is held past the pipeline thread's wait. The thread
used to give up, record the run as failed and discard its outputs, while the
held write was still on its way. Only the hold and the length of one wait are
test-made; nothing else is replaced.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.web.app import create_app
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.execution import service as service_module
from elspeth.web.sessions.models import runs_table
from elspeth.web.sessions.protocol import SESSION_TERMINAL_RUN_STATUS_VALUES, CompositionStateData
from elspeth.web.sessions.telemetry import _FakeCounter
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.unit.web.execution.test_loop_bridge import _ImpatientFuture

TEST_CSV = Path(__file__).parent / "fixtures" / "test_input.csv"
_BOUND_SECONDS = 30.0
_SCHEMA = {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]}


@dataclass
class _HeldRun:
    """A started run whose first terminal status write is held until ``release`` is set."""

    client: AsyncClient
    headers: dict[str, str]
    run_id: str
    output_file: Path
    session_engine: Engine
    app_lifetime: AsyncExitStack
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    waits: list[float] = field(default_factory=list)
    statuses_written: list[str] = field(default_factory=list)

    async def status(self) -> dict[str, Any]:
        response = await self.client.get(f"/api/runs/{self.run_id}", headers=self.headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def stored_status(self) -> str:
        """The run row's status, read straight from the session store."""
        with self.session_engine.connect() as connection:
            return str(connection.execute(select(runs_table.c.status).where(runs_table.c.id == self.run_id)).scalar_one())

    def terminal_writes(self) -> list[str]:
        return [status for status in self.statuses_written if status in SESSION_TERMINAL_RUN_STATUS_VALUES]

    async def until(self, condition: Any) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BOUND_SECONDS
        while not condition() and loop.time() < deadline:
            await asyncio.sleep(0.02)

    async def until_waits_ran_out(self, count: int) -> str:
        """Let the wait for the held write run out ``count`` more times, unless the run goes terminal first."""
        target = len(self.waits) + count
        status = (await self.status())["status"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BOUND_SECONDS
        while len(self.waits) < target and status not in SESSION_TERMINAL_RUN_STATUS_VALUES and loop.time() < deadline:
            await asyncio.sleep(0.02)
            status = (await self.status())["status"]
        return status


@asynccontextmanager
async def _run_with_held_terminal_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, slow_calls: _FakeCounter | None = None
) -> AsyncIterator[_HeldRun]:
    for name in ("blobs", "outputs", "runs"):
        (tmp_path / name).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app = create_app(settings=settings)
    app.state.auth_provider.create_user("testuser", "testpass123", display_name="Test User")
    wire_test_pipeline_user_authority(app, identity_id="testuser", engine=app.state.session_engine)

    async with AsyncExitStack() as app_lifetime:
        await app_lifetime.enter_async_context(LifespanManager(app))
        client = await app_lifetime.enter_async_context(AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
        login = await client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = await client.post("/api/sessions", headers=headers, json={"title": "held terminal write"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        blob_dir = tmp_path / "blobs" / session_id
        blob_dir.mkdir(parents=True)
        shutil.copy(TEST_CSV, blob_dir / "input.csv")
        output_file = tmp_path / "outputs" / session_id / "result.csv"
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="primary",
                options={"path": str(blob_dir / "input.csv"), "schema": _SCHEMA},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(name="primary", plugin="csv", options={"path": str(output_file), "schema": _SCHEMA}, on_write_failure="discard"),
            ),
            metadata=PipelineMetadata(name="Held terminal write", description="CSV passthrough"),
            version=1,
        ).to_dict()
        session_service = app.state.session_service
        await _save_composition_state_with_compose_authority(
            session_service,
            UUID(session_id),
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=state["edges"],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )

        if slow_calls is not None:
            # The app wires real meters; read this one counter through the recording kind.
            monkeypatch.setattr(
                app.state.execution_service,
                "_telemetry",
                replace(app.state.execution_service._telemetry, execution_loop_bridge_slow_total=slow_calls),
            )
        run = _HeldRun(
            client=client,
            headers=headers,
            run_id="",
            output_file=output_file,
            session_engine=app.state.session_engine,
            app_lifetime=app_lifetime,
        )
        real_update = session_service.update_run_status

        async def held_update_run_status(run_id: UUID, status: str, *args: Any, **kwargs: Any) -> None:
            if status in SESSION_TERMINAL_RUN_STATUS_VALUES and not run.held.is_set():
                run.held.set()
                await run.release.wait()
            try:
                await real_update(run_id, status, *args, **kwargs)
            finally:
                run.statuses_written.append(status)

        monkeypatch.setattr(session_service, "update_run_status", held_update_run_status)

        # Cut the pipeline thread's wait for that one write to 50 ms a time.
        real_schedule = asyncio.run_coroutine_threadsafe

        def schedule(coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> Any:
            future = real_schedule(coro, loop)
            return _ImpatientFuture(future, run.waits) if coro.cr_code is held_update_run_status.__code__ else future  # type: ignore[attr-defined]

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)

        started = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
        assert started.status_code == 202, started.text
        run.run_id = started.json()["run_id"]
        # The pipeline finishes its work and writes its output before it asks for the terminal status.
        await asyncio.wait_for(run.held.wait(), _BOUND_SECONDS)
        try:
            yield run
        finally:
            run.release.set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_held_terminal_status_write_ends_as_the_one_status_the_pipeline_earned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _run_with_held_terminal_write(tmp_path, monkeypatch) as run:
        status_while_held = await run.until_waits_ran_out(3)
        run.release.set()
        # An abandoned write lands late; wait for it before the last reading.
        await run.until(lambda: "completed" in run.statuses_written)
        final = await run.status()

        assert {
            "status while the write was held": status_while_held,
            "final status": final["status"],
            "terminal writes attempted": run.terminal_writes(),
            "output kept": run.output_file.exists(),
        } == {
            "status while the write was held": "running",
            "final status": "completed",
            "terminal writes attempted": ["completed"],
            "output kept": True,
        }, final.get("error")
        assert final["accounting"]["source"]["rows_processed"] > 0
        assert final["accounting"]["tokens"]["failed"] == 0
        results = await run.client.get(f"/api/runs/{run.run_id}/results", headers=run.headers)
        assert results.status_code == 200, results.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelling_while_the_terminal_write_is_held_returns_and_the_run_still_ends_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_LOOP_BRIDGE_SLOW_SECONDS", 0.0)
    slow_calls = _FakeCounter()
    async with _run_with_held_terminal_write(tmp_path, monkeypatch, slow_calls=slow_calls) as run:
        assert await run.until_waits_ran_out(3) == "running"

        # The cancel request must not wait behind the held write.
        cancel = await asyncio.wait_for(run.client.post(f"/api/runs/{run.run_id}/cancel", headers=run.headers), _BOUND_SECONDS)
        assert cancel.status_code in (200, 202), cancel.text
        run.release.set()
        await run.until(lambda: "completed" in run.statuses_written)
        final = await run.status()

        # The pipeline had already finished, so the request changes nothing: one terminal
        # status, the one the pipeline earned, and the stored row agrees.
        assert final["status"] == "completed", final.get("error")
        assert run.terminal_writes() == ["completed"]
        assert run.stored_status() == "completed"
        # The long wait was counted once, against this run and the call it waited on.
        assert [(amount, attributes) for amount, attributes, _context in slow_calls.calls] == [
            (1, {"run_id": run.run_id, "operation": "_run_with_held_terminal_write.<locals>.held_update_run_status"})
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shutting_down_while_the_terminal_write_is_held_waits_for_it_and_no_longer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _run_with_held_terminal_write(tmp_path, monkeypatch) as run:
        assert await run.until_waits_ran_out(3) == "running"

        shutdown = asyncio.create_task(run.app_lifetime.aclose())
        # Shutdown is in progress and the loop is still running, so the pipeline thread keeps waiting.
        waits_before = len(run.waits)
        await run.until(lambda: len(run.waits) >= waits_before + 3 or shutdown.done())
        shutdown_ended_early = shutdown.done()
        run.release.set()
        await asyncio.wait_for(shutdown, _BOUND_SECONDS)

        assert not shutdown_ended_early
        assert run.terminal_writes() == ["completed"]
        # Shutdown closes the worker pool first, so the released write is refused and the
        # run stays non-terminal for restart recovery. Either way it is never recorded
        # with a terminal status the pipeline did not earn.
        assert run.stored_status() in ("completed", "running")
