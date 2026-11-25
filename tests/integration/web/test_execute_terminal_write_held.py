"""A slow write to the session store must not turn a run into something it was not.

Through the real routes, execution service, engine, session store and run
recovery: one of the run's writes is held past the pipeline thread's wait.
The thread used to give up after 30 seconds, record the run as failed, and
leave the held write on its way. Only the hold, the length of one wait and
the bounds are test-made; nothing else is replaced.
"""

from __future__ import annotations

import asyncio
import faulthandler
import functools
import shutil
import sys
import threading
import traceback
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import CoroutineType
from typing import Any, Literal
from uuid import UUID

import pytest
import structlog
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.contracts.errors import GracefulShutdownError
from elspeth.web.app import create_app
from elspeth.web.async_workers import run_lease_upkeep_in_worker
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.coordination.upkeep_engine import lease_upkeep_authority
from elspeth.web.execution import service as service_module
from elspeth.web.execution.recovery import RunRecoveryCoordinator
from elspeth.web.sessions.models import run_events_table, runs_table, session_operation_fences_table
from elspeth.web.sessions.protocol import SESSION_TERMINAL_RUN_STATUS_VALUES, CompositionStateData
from elspeth.web.sessions.telemetry import _FakeCounter
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.unit.web.execution.test_loop_bridge import _ImpatientFuture

TEST_CSV = Path(__file__).parent / "fixtures" / "test_input.csv"
_BOUND_SECONDS = 30.0
_SCHEMA = {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]}


_TERMINAL_EVENT_TYPES = ("completed", "failed", "cancelled")
_SCENARIO_BOUND_SECONDS = 90.0


#: What a scenario that never returned had got to; read only when the bound is hit.
_SCENARIO_NOTES: list[str] = []
_LIVE_REGISTRIES: list[Any] = []


def _lease_registry_notes(registry: Any) -> list[str]:
    obligations = tuple(registry._pending.values())
    notes = [f"execution lease registry: {len(obligations)} pending obligation(s)"]
    for obligation in obligations:
        release = obligation.release_submission
        notes.append(
            "unretired lease obligation: "
            f"release observed={release is not None and release.observed}, "
            f"release ended with={None if release is None else release.original_error!r}, "
            f"succeeded={obligation.release_succeeded}, lost={obligation.release_lost}, "
            f"completion={obligation.completion_required}/{obligation.completion_observed}, "
            f"lifecycle={obligation.lifecycle_required}/{obligation.lifecycle_observed}"
        )
    return notes


@asynccontextmanager
async def _preserve_setup_failure(failures: list[BaseException]) -> AsyncIterator[None]:
    try:
        yield
    except BaseException as closing:
        if not failures:
            raise
        original = failures[0]
        if closing is not original:
            original.add_note(f"app shutdown also failed with {type(closing).__name__}: {closing}")
            raise original from closing
        raise


def _bounded(scenario: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async scenario, app shutdown included, on its own loop in a daemon thread.

    These scenarios leave a run thread waiting on purpose. If the code under
    test then never lets go, or the app's shutdown never returns, the test
    must fail here instead of holding the test process.
    """

    @functools.wraps(scenario)
    def run(*args: Any, **kwargs: Any) -> None:
        outcome: list[BaseException | None] = []
        loop = asyncio.new_event_loop()
        _SCENARIO_NOTES.clear()
        _LIVE_REGISTRIES.clear()

        def target() -> None:
            try:
                loop.run_until_complete(scenario(*args, **kwargs))
            except BaseException as raised:
                outcome.append(raised)
            else:
                outcome.append(None)

        thread = threading.Thread(target=target, name="held-write-scenario", daemon=True)
        thread.start()
        thread.join(_SCENARIO_BOUND_SECONDS)
        if not outcome:
            # Say where it is waiting: every unfinished task on the scenario's loop, innermost frame last.
            waiting = []
            for task in asyncio.all_tasks(loop):
                frames = [f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}" for frame in task.get_stack(limit=6)]
                waiting.append(f"{task.get_name()}: " + " > ".join(frames))
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            waiting.extend(_SCENARIO_NOTES)
            for registry in _LIVE_REGISTRIES:
                waiting.extend(_lease_registry_notes(registry))
            pytest.fail(
                f"the scenario, or the app shutdown that ends it, did not return within {_SCENARIO_BOUND_SECONDS:.0f} s. "
                "Unfinished tasks:\n" + "\n".join(sorted(waiting))
            )
        loop.close()
        if outcome[0] is not None:
            raise outcome[0]

    return run


@dataclass
class _HeldRun:
    """A started run with one of its session-store writes held until ``release`` is set."""

    app: Any
    settings: WebSettings
    client: AsyncClient
    headers: dict[str, str]
    session_id: str
    run_id: str
    output_file: Path
    session_engine: Engine
    app_lifetime: AsyncExitStack
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when the held write is cancelled by the bridge instead of released.
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    waits: list[float] = field(default_factory=list)
    #: The ``success`` flag of every output finalisation the run thread started.
    output_finalisations: list[bool] = field(default_factory=list)
    #: Every status write the run thread made, with how it ended: "written" or the refusal's class name.
    status_writes: list[tuple[str, str]] = field(default_factory=list)
    #: Every call sent through the bridge, by name, in order.
    bridged: list[str] = field(default_factory=list)
    bridged_when_cancelled: int | None = None
    #: Heartbeats refused after ``stop_lease_renewal``.
    renewals_refused: int = 0
    #: The upkeep authority's own renew, kept when the heartbeat is stopped.
    unstopped_renew: Any = None

    async def status(self) -> dict[str, Any]:
        response = await self.client.get(f"/api/runs/{self.run_id}", headers=self.headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def stored_status(self) -> str:
        """The run row's status, read straight from the session store."""
        with self.session_engine.connect() as connection:
            return str(connection.execute(select(runs_table.c.status).where(runs_table.c.id == self.run_id)).scalar_one())

    def stored_terminal_events(self) -> list[str]:
        with self.session_engine.connect() as connection:
            rows = connection.execute(
                select(run_events_table.c.event_type).where(run_events_table.c.run_id == self.run_id).order_by(run_events_table.c.sequence)
            )
            return [row.event_type for row in rows if row.event_type in _TERMINAL_EVENT_TYPES]

    def lease_has_lapsed(self) -> bool:
        """The fence ran past its deadline while still held; a released fence has not lapsed."""
        fences = session_operation_fences_table
        with self.session_engine.connect() as connection:
            fence = connection.execute(
                select(fences.c.lease_expires_at, fences.c.released_at).where(fences.c.session_id == self.session_id)
            ).one()
        if fence.released_at is not None:
            return False
        expires = fence.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return bool(expires + timedelta(seconds=0.3) < datetime.now(UTC))

    def stop_lease_renewal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the next heartbeat fail, as it does when the store that stalled the write stalls it too.

        The heartbeat renews through the lease-upkeep authority, so that is the
        one stopped; ``renewals_refused`` counts the heartbeats it refused.
        """

        def renewal_stalled(*_args: Any, **_kwargs: Any) -> Any:
            self.renewals_refused += 1
            raise RuntimeError("the test stopped lease renewal")

        upkeep = lease_upkeep_authority(self.app.state.session_service.session_operation_authority)
        self.unstopped_renew = upkeep.renew
        monkeypatch.setattr(upkeep, "renew", renewal_stalled)

    async def shorten_lease(self, lease_seconds: int) -> None:
        """Renew the run's lease once more, for ``lease_seconds``, past the stopped heartbeat.

        Call it only after a heartbeat was refused: a refused renewal ends the
        heartbeat, so no renewal still in flight can extend the lease again.
        """
        assert self.renewals_refused >= 1, "the heartbeat must have stopped first"
        registry = self.app.state.execution_service.execution_lease_release_registry
        (obligation,) = tuple(registry._pending.values())
        assert obligation.context is not None
        await run_lease_upkeep_in_worker(self.unstopped_renew, obligation.context, lease_seconds=lease_seconds)

    def drop_the_unreleased_lease_obligation(self) -> None:
        """Let this test's app close after its lease ran out under a live thread.

        A release refused because the lease expired is neither a release nor a
        recognised loss to the lease registry, so the app's shutdown waits on
        it for good. A deployed process is ended by its watchdog instead; a
        test has to drop the obligation by hand.
        """
        registry = self.app.state.execution_service.execution_lease_release_registry
        with registry._lock:
            registry._pending.clear()

    def fence_epoch(self) -> int:
        with self.session_engine.connect() as connection:
            return int(
                connection.execute(
                    select(session_operation_fences_table.c.operation_epoch).where(
                        session_operation_fences_table.c.session_id == self.session_id
                    )
                ).scalar_one()
            )

    def audit_status(self) -> str | None:
        """The run's status in the audit store, as recovery reads it."""
        from elspeth.web.execution.recovery import observe_run

        observation = observe_run(str(self.settings.landscape_url), self.run_id, create_tables=False)
        return None if observation.status is None else observation.status.value

    def terminal_writes(self) -> list[tuple[str, str]]:
        return [(status, ended) for status, ended in self.status_writes if status in SESSION_TERMINAL_RUN_STATUS_VALUES]

    def a_terminal_write_ended(self, status: str) -> bool:
        return any(written == status for written, _ended in self.terminal_writes())

    def sent_after_the_give_up(self) -> list[str]:
        """Calls the pipeline thread sent after its held call was cancelled, apart from handing back its lease."""
        assert self.bridged_when_cancelled is not None
        return [name for name in self.bridged[self.bridged_when_cancelled :] if not name.endswith("._finish_execution_authority")]

    def thread_has_left_the_run(self) -> bool:
        return self.run_id not in self.app.state.execution_service.get_live_run_ids()

    async def recover(self) -> None:
        """One pass of the run recovery the server makes at startup and on its sweep."""
        await RunRecoveryCoordinator(
            self.app.state.session_service,
            self.app.state.execution_service,
            self.app.state.blob_service,
            landscape_url=str(self.settings.landscape_url),
            create_tables=True,
            landscape_passphrase=self.settings.landscape_passphrase,
        ).recover()

    async def until(self, condition: Any) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BOUND_SECONDS
        while not condition() and loop.time() < deadline:
            await asyncio.sleep(0.02)
        assert condition(), "the awaited condition did not come about"

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
async def _run_with_a_held_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hold: Literal["terminal status", "progress event"] = "terminal status",
    slow_calls: _FakeCounter | None = None,
    give_up_seconds: float | None = None,
) -> AsyncIterator[_HeldRun]:
    """Start a run whose first write of the ``hold`` kind is held until ``release`` is set.

    ``give_up_seconds`` shortens the bridge's give-up bound only once the held
    write has been sent. Shortening it for the whole run would also cut an
    ordinary call that runs slowly (a first, cold call under coverage takes
    longer than 0.25 s), and the run would end before it reached the held write.
    The run's lease keeps its ordinary length for the same reason; a test that
    needs it short shortens it on the held run (``_HeldRun.shorten_lease``).
    """
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

    setup_failures: list[BaseException] = []
    async with _preserve_setup_failure(setup_failures), AsyncExitStack() as app_lifetime:
        await app_lifetime.enter_async_context(LifespanManager(app))
        client = await app_lifetime.enter_async_context(AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
        login = await client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = await client.post("/api/sessions", headers=headers, json={"title": "held write"})
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
            metadata=PipelineMetadata(name="Held write", description="CSV passthrough"),
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
            app=app,
            settings=settings,
            client=client,
            headers=headers,
            session_id=session_id,
            run_id="",
            output_file=output_file,
            session_engine=app.state.session_engine,
            app_lifetime=app_lifetime,
        )

        async def hold_here() -> None:
            if give_up_seconds is not None:
                monkeypatch.setattr(service_module, "_LOOP_BRIDGE_GIVE_UP_SECONDS", give_up_seconds)
            run.held.set()
            try:
                await run.release.wait()
            except asyncio.CancelledError:
                run.bridged_when_cancelled = len(run.bridged)
                run.cancelled.set()
                raise

        real_update = session_service.update_run_status
        real_append = session_service.append_run_event

        async def held_update_run_status(run_id: UUID, status: str, *args: Any, **kwargs: Any) -> None:
            if hold == "terminal status" and status in SESSION_TERMINAL_RUN_STATUS_VALUES and not run.held.is_set():
                await hold_here()
            try:
                await real_update(run_id, status, *args, **kwargs)
            except BaseException as refusal:
                run.status_writes.append((status, type(refusal).__name__))
                raise
            run.status_writes.append((status, "written"))

        async def held_append_run_event(*args: Any, event_type: str, **kwargs: Any) -> Any:
            if hold == "progress event" and event_type == "progress" and not run.held.is_set():
                await hold_here()
            return await real_append(*args, event_type=event_type, **kwargs)

        monkeypatch.setattr(session_service, "update_run_status", held_update_run_status)
        monkeypatch.setattr(session_service, "append_run_event", held_append_run_event)

        # Record every bridged call, and cut the pipeline thread's waits for the held kind to 50 ms a time.
        held_code = held_update_run_status.__code__ if hold == "terminal status" else held_append_run_event.__code__
        real_schedule = asyncio.run_coroutine_threadsafe

        def schedule(coro: CoroutineType[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> Any:
            run.bridged.append(coro.__qualname__)
            future = real_schedule(coro, loop)
            return _ImpatientFuture(future, run.waits) if coro.cr_code is held_code else future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)

        real_finalize = app.state.execution_service._finalize_output_blobs

        def finalize(run_id: str, *, success: bool, **kwargs: Any) -> Any:
            run.output_finalisations.append(success)
            return real_finalize(run_id, success=success, **kwargs)

        monkeypatch.setattr(app.state.execution_service, "_finalize_output_blobs", finalize)
        lease_registry = app.state.execution_service.execution_lease_release_registry
        _LIVE_REGISTRIES.append(lease_registry)
        real_begin = app.state.process_recovery._begin

        def begin(reason: Any, *, signal_target: bool) -> None:
            where = " > ".join(
                f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}" for frame in traceback.extract_stack()[-7:-1]
            )
            _SCENARIO_NOTES.append(f"process recovery began: {reason} from {where}")
            real_begin(reason, signal_target=signal_target)

        monkeypatch.setattr(app.state.process_recovery, "_begin", begin)
        lease_registry = app.state.execution_service.execution_lease_release_registry
        real_record_failure = lease_registry.record_failure

        def record_failure(original: BaseException) -> None:
            _SCENARIO_NOTES.append(f"lease registry failure: {original!r}")
            real_record_failure(original)

        monkeypatch.setattr(lease_registry, "record_failure", record_failure)
        try:
            started = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
            assert started.status_code == 202, started.text
            run.run_id = started.json()["run_id"]
            try:
                await asyncio.wait_for(run.held.wait(), _BOUND_SECONDS)
            except TimeoutError:
                pytest.fail(f"the run did not reach the held write within {_BOUND_SECONDS:.0f} s; bridged calls so far: {run.bridged}")
        except BaseException as raised:
            note = f"held-write setup failed with {type(raised).__name__}: {raised}"
            _SCENARIO_NOTES.append(note)
            raised.add_note("\n".join([note, *_lease_registry_notes(lease_registry)]))
            setup_failures.append(raised)
            run.drop_the_unreleased_lease_obligation()
            run.release.set()
            raise
        try:
            yield run
        except BaseException as raised:
            where = " > ".join(
                f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}" for frame in traceback.extract_tb(raised.__traceback__)
            )
            _SCENARIO_NOTES.append(f"the test body had already ended with {type(raised).__name__}: {raised} at {where}")
            raised.add_note("\n".join(_lease_registry_notes(lease_registry)))
            run.drop_the_unreleased_lease_obligation()
            raise
        else:
            _SCENARIO_NOTES.append("the test body had already finished; the app shutdown did not return")
        finally:
            run.release.set()


@pytest.mark.integration
@_bounded
async def test_a_held_terminal_status_write_ends_as_the_one_status_the_pipeline_earned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _run_with_a_held_write(tmp_path, monkeypatch) as run:
        status_while_held = await run.until_waits_ran_out(3)
        run.release.set()
        # An abandoned write lands late; wait for it before the last reading.
        await run.until(lambda: run.a_terminal_write_ended("completed"))
        final = await run.status()

        assert {
            "status while the write was held": status_while_held,
            "final status": final["status"],
            "terminal writes by the run thread": run.terminal_writes(),
            "output kept": run.output_file.exists(),
        } == {
            "status while the write was held": "running",
            "final status": "completed",
            "terminal writes by the run thread": [("completed", "written")],
            "output kept": True,
        }, final.get("error")
        assert final["accounting"]["source"]["rows_processed"] > 0
        assert final["accounting"]["tokens"]["failed"] == 0
        results = await run.client.get(f"/api/runs/{run.run_id}/results", headers=run.headers)
        assert results.status_code == 200, results.text


async def _recover_until_settled(run: _HeldRun) -> None:
    """Recovery passes, as the sweep makes them, until the run's stored status is terminal."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _BOUND_SECONDS
    await run.recover()
    while run.stored_status() not in SESSION_TERMINAL_RUN_STATUS_VALUES and loop.time() < deadline:
        await asyncio.sleep(0.05)
        await run.recover()


@pytest.mark.integration
@_bounded
async def test_a_terminal_write_that_never_finishes_frees_the_thread_and_recovery_records_what_the_pipeline_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with structlog.testing.capture_logs() as logs:
        async with _run_with_a_held_write(tmp_path, monkeypatch, give_up_seconds=0.2) as run:
            # The write is never released. The thread must leave the run by itself.
            await run.until(run.thread_has_left_the_run)
            await run.until(run.cancelled.is_set)
            left_behind = {
                "stored status": run.stored_status(),
                "audit store status": run.audit_status(),
                "terminal writes by the run thread": run.terminal_writes(),
                "terminal events stored": run.stored_terminal_events(),
                "calls sent after the give-up": run.sent_after_the_give_up(),
                "failure handling started": False in run.output_finalisations,
                "told the operator the pipeline failed": [entry["event"] for entry in logs if entry["event"] == "run_pipeline_failed"],
            }

            # Recovery takes the run once the thread's lease has been handed back.
            await _recover_until_settled(run)
            final = await run.status()

            assert left_behind == {
                "stored status": "running",
                "audit store status": "completed",
                "terminal writes by the run thread": [],
                "terminal events stored": [],
                "calls sent after the give-up": [],
                "failure handling started": False,
                "told the operator the pipeline failed": [],
            }
            assert {
                "final status": final["status"],
                "terminal events stored": run.stored_terminal_events(),
                "terminal writes by the run thread": run.terminal_writes(),
                "output kept": run.output_file.exists(),
            } == {
                "final status": "completed",
                "terminal events stored": ["completed"],
                "terminal writes by the run thread": [],
                "output kept": True,
            }, final.get("error")
            assert final["accounting"]["source"]["rows_processed"] > 0


@pytest.mark.integration
@_bounded
async def test_a_progress_write_that_never_finishes_inside_the_engine_ends_with_both_stores_agreeing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine records its own failure for the run before the error gets back to the run thread.

    So the audit store says ``failed`` while the session store still says
    ``running``. The run thread must add nothing, and recovery must carry the
    audit store's answer across.
    """
    with structlog.testing.capture_logs() as logs:
        async with _run_with_a_held_write(tmp_path, monkeypatch, hold="progress event", give_up_seconds=0.2) as run:
            await run.until(run.thread_has_left_the_run)
            await run.until(run.cancelled.is_set)
            left_behind = {
                "stored status": run.stored_status(),
                "audit store status": run.audit_status(),
                "terminal writes by the run thread": run.terminal_writes(),
                "terminal events stored": run.stored_terminal_events(),
                "calls sent after the give-up": run.sent_after_the_give_up(),
                "failure handling started": False in run.output_finalisations,
                "told the operator the pipeline failed": [entry["event"] for entry in logs if entry["event"] == "run_pipeline_failed"],
            }

            await _recover_until_settled(run)

            assert left_behind == {
                "stored status": "running",
                "audit store status": "failed",
                "terminal writes by the run thread": [],
                "terminal events stored": [],
                "calls sent after the give-up": [],
                "failure handling started": False,
                "told the operator the pipeline failed": [],
            }
            assert {
                "stored status": run.stored_status(),
                "audit store status": run.audit_status(),
                "terminal events stored": run.stored_terminal_events(),
                "terminal writes by the run thread": run.terminal_writes(),
            } == {
                "stored status": "failed",
                "audit store status": "failed",
                "terminal events stored": ["failed"],
                "terminal writes by the run thread": [],
            }


@pytest.mark.integration
@_bounded
async def test_a_give_up_the_engine_reports_as_a_shutdown_is_still_not_recorded_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine layer may swallow the bridge's error and end the run its own way.

    Here it ends as a graceful shutdown, which the run thread normally
    records as ``cancelled``. After an unsettled call it must record nothing.
    """
    real_run = service_module.Orchestrator.run

    def run_that_reports_a_shutdown(orchestrator: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return real_run(orchestrator, *args, **kwargs)
        except RuntimeError as swallowed:
            raise GracefulShutdownError(1, "engine-run") from swallowed

    monkeypatch.setattr(service_module.Orchestrator, "run", run_that_reports_a_shutdown)
    async with _run_with_a_held_write(tmp_path, monkeypatch, hold="progress event", give_up_seconds=0.2) as run:
        await run.until(run.thread_has_left_the_run)
        await run.until(run.cancelled.is_set)

        assert {
            "stored status": run.stored_status(),
            "terminal writes by the run thread": run.terminal_writes(),
            "terminal events stored": run.stored_terminal_events(),
            "calls sent after the give-up": run.sent_after_the_give_up(),
            "failure handling started": False in run.output_finalisations,
        } == {
            "stored status": "running",
            "terminal writes by the run thread": [],
            "terminal events stored": [],
            "calls sent after the give-up": [],
            "failure handling started": False,
        }


@pytest.mark.integration
@_bounded
async def test_cancelling_while_the_terminal_write_is_held_returns_and_the_run_still_ends_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_LOOP_BRIDGE_SLOW_SECONDS", 0.0)
    slow_calls = _FakeCounter()
    async with _run_with_a_held_write(tmp_path, monkeypatch, slow_calls=slow_calls) as run:
        assert await run.until_waits_ran_out(3) == "running"

        # The cancel request must not wait behind the held write.
        cancel = await asyncio.wait_for(run.client.post(f"/api/runs/{run.run_id}/cancel", headers=run.headers), _BOUND_SECONDS)
        assert cancel.status_code in (200, 202), cancel.text
        run.release.set()
        await run.until(lambda: run.a_terminal_write_ended("completed"))
        final = await run.status()

        # The pipeline had already finished, so the request changes nothing: one terminal
        # status, the one the pipeline earned, and the stored row agrees.
        assert final["status"] == "completed", final.get("error")
        assert run.terminal_writes() == [("completed", "written")]
        assert run.stored_status() == "completed"
        # The long wait was counted once, against this run and the call it waited on.
        assert [(amount, attributes) for amount, attributes, _context in slow_calls.calls] == [
            (1, {"run_id": run.run_id, "operation": "_run_with_a_held_write.<locals>.held_update_run_status"})
        ]


@pytest.mark.integration
@_bounded
async def test_a_lapsed_lease_neither_lets_recovery_take_a_waiting_run_nor_lets_its_late_write_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached_the_end: list[bool] = []
    # The app reports the lost lease when it closes; the assertions inside must still all have run.
    with pytest.raises(ExceptionGroup, match="Application lifecycle obligations failed"):
        async with _run_with_a_held_write(tmp_path, monkeypatch) as run:
            epoch = run.fence_epoch()
            # The stall that holds the write also stops the heartbeat, so the lease runs out
            # while the thread is still waiting on the bridge. The heartbeat really is stopped
            # where it renews, and its first refusal ends it; only then is the lease cut to 2 s,
            # so no renewal still in flight can extend it again.
            run.stop_lease_renewal(monkeypatch)
            await run.until(lambda: run.renewals_refused >= 1)
            await run.shorten_lease(2)
            await run.until(run.lease_has_lapsed)

            await run.recover()

            # (i) Nothing but the thread's presence protects the run now, and it is enough.
            assert {
                "thread still on the run": not run.thread_has_left_the_run(),
                "stored status": run.stored_status(),
                "fence epoch unchanged": run.fence_epoch() == epoch,
                "terminal events stored": run.stored_terminal_events(),
            } == {
                "thread still on the run": True,
                "stored status": "running",
                "fence epoch unchanged": True,
                "terminal events stored": [],
            }

            # (ii) The held write reaches the store after the lapse: refused, and the thread writes nothing else.
            run.release.set()
            await run.until(run.thread_has_left_the_run)
            assert {
                "terminal writes by the run thread": run.terminal_writes(),
                "stored status": run.stored_status(),
                "terminal events stored": run.stored_terminal_events(),
                "fence epoch unchanged": run.fence_epoch() == epoch,
            } == {
                "terminal writes by the run thread": [("completed", "SessionOperationFenceLost")],
                "stored status": "running",
                "terminal events stored": [],
                "fence epoch unchanged": True,
            }

            # A lease lost under a live thread is a fault of this process: it stops taking work and
            # asks to be restarted. The run stays as it is for the next process's startup recovery.
            await run.until(run.app.state.instance_draining.is_set)
            assert run.stored_status() == "running"
            run.drop_the_unreleased_lease_obligation()
            reached_the_end.append(True)
    assert reached_the_end


@pytest.mark.integration
@_bounded
async def test_shutting_down_while_the_terminal_write_is_held_waits_for_it_and_no_longer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _run_with_a_held_write(tmp_path, monkeypatch) as run:
        assert await run.until_waits_ran_out(3) == "running"

        shutdown = asyncio.create_task(run.app_lifetime.aclose())
        # Shutdown is in progress and the loop is still running, so the pipeline thread keeps waiting.
        waits_before = len(run.waits)
        await run.until(lambda: len(run.waits) >= waits_before + 3 or shutdown.done())
        shutdown_ended_early = shutdown.done()
        run.release.set()
        await shutdown

        assert not shutdown_ended_early
        # Shutdown closes the worker pool first, so the released write is refused and the run
        # stays non-terminal for restart recovery. Whichever way it went, the stored status is
        # never one the pipeline did not earn.
        ((status, ended),) = run.terminal_writes()
        assert status == "completed"
        assert run.stored_status() == ("completed" if ended == "written" else "running")
