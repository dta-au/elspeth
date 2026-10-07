"""Prospective, unrun fixture ownership tests; one selected node per child.

The existing core module supplies its exact f25 lifecycle fixture as a pytest
plugin.  These cases add observations without changing that fixture or its
seventeen business test functions.  Each child is selected alone.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from elspeth.web import async_workers
from tests.helpers import composer_operations
from tests.unit.web import test_execute_lease_cleanup_core as core

# Re-export the exact reviewed FixtureFunctionDefinition under this module's
# nodeid. A pytest_plugins declaration would register its autouse fixture at
# session root and apply f25 custody to unrelated collected tests.
_owned_core_lifecycle = core._owned_core_lifecycle

SCENARIOS: dict[str, object] = {}


@pytest.fixture(scope="module", autouse=True)
def _requires_owned_core_edge_probe(request):
    """Run deliberate-red cases only under their explicit finite owner."""
    if not request.config.pluginmanager.hasplugin("core_fixture_edges_probe_plugin"):
        pytest.skip("core edge diagnostic requires its owned finite probe")


@pytest.mark.asyncio
async def test_zero_attempt_inherited_physical_owner() -> None:
    """The no-app branch still joins a real, already-started worker."""
    shared = async_workers._get_shared_executor()
    generation = async_workers._generation_for(shared)
    future = shared.submit(lambda: "real inherited worker")
    assert future.result(timeout=3) == "real inherited worker"
    threads = tuple(shared._threads)
    assert threads and all(thread.ident is not None for thread in threads)
    SCENARIOS["zero_attempt"] = (shared, generation, threads, future)
    # The imported f25 autouse fixture owns teardown and must flag the inherited
    # generation before joining/detaching the actual physical worker.


@pytest.mark.asyncio
async def test_factory_no_return_before_app_allocation(tmp_path) -> None:
    """The early plugin replaces the factory before f25 captures its callable."""
    marker = SCENARIOS["factory_original"]
    assert isinstance(marker, RuntimeError)
    with pytest.raises(RuntimeError) as raised:
        await composer_operations.build_composer_operation_app(tmp_path, timeout_seconds=30.0)
    assert raised.value is marker
    # The f25 owner observed one call but no returned app and must retain
    # allocation Unknown at teardown. No app was constructed by this mode.


@pytest.mark.asyncio
async def test_returned_app_seed_failure_keeps_app_and_worker_custody(tmp_path, monkeypatch) -> None:
    """The app returned to f25 before the helper's session seed fails."""
    from elspeth.web.sessions.service import SessionServiceImpl

    seed_original = RuntimeError("returned app seed original")

    async def fail_session_seed(self, *args, **kwargs):
        raise seed_original

    captured_app = []
    f25_capture = composer_operations.create_app

    def observe_f25_returned_app(*args, **kwargs):
        app = f25_capture(*args, **kwargs)
        captured_app.append(app)
        return app

    monkeypatch.setattr(SessionServiceImpl, "create_session", fail_session_seed)
    monkeypatch.setattr(composer_operations, "create_app", observe_f25_returned_app)
    with pytest.raises(RuntimeError) as raised:
        await composer_operations.build_composer_operation_app(tmp_path, timeout_seconds=30.0)
    assert raised.value is seed_original
    assert len(captured_app) == 1
    app = captured_app[0]
    pool_before_dispose = app.state.session_engine.pool
    shared = async_workers._get_shared_executor()
    generation = async_workers._generation_for(shared)
    future = shared.submit(lambda: "real returned-app worker")
    assert future.result(timeout=3) == "real returned-app worker"
    threads = tuple(shared._threads)
    assert threads and all(thread.ident is not None for thread in threads)
    SCENARIOS["returned_seed"] = (app, pool_before_dispose, generation, shared, threads, future, seed_original)
    # f25 retains the actual returned app, owns its session engine, and joins
    # this worker even though the helper never returned its fixture wrapper.


@pytest.mark.asyncio
async def test_body_and_worker_shutdown_keep_both_originals(tmp_path, monkeypatch) -> None:
    """A real app/shutdown retains the exact body and post-join originals."""
    fixture, _registry = await core._core(tmp_path)
    shared = async_workers._get_shared_executor()
    generation = async_workers._generation_for(shared)
    future = shared.submit(lambda: "real shutdown worker")
    assert future.result(timeout=3) == "real shutdown worker"
    threads = tuple(shared._threads)
    assert threads and all(thread.ident is not None for thread in threads)
    body_original = RuntimeError("core edge body original")
    cleanup_original = ValueError("core edge cleanup original after actual join")
    actual_shutdown = async_workers.shutdown_async_workers

    async def fail_after_actual_join() -> None:
        await actual_shutdown()
        raise cleanup_original

    monkeypatch.setattr(async_workers, "shutdown_async_workers", fail_after_actual_join)
    SCENARIOS["body_cleanup"] = (fixture.app, generation, shared, threads, body_original, cleanup_original)
    raise body_original


@pytest.mark.asyncio
async def test_held_generation_observer_joins_present_drain_and_replacement(tmp_path, monkeypatch) -> None:
    """Fixture observation releases a real held drain, then joins both pools."""
    fixture, _registry = await core._core(tmp_path)
    shared = async_workers._get_shared_executor()
    generation = async_workers._generation_for(shared)
    future = shared.submit(lambda: "old actual worker")
    assert future.result(timeout=3) == "old actual worker"
    old_threads = tuple(shared._threads)
    assert old_threads and all(thread.ident is not None for thread in old_threads)
    entered = threading.Event()
    release = threading.Event()
    original_shutdown = shared.shutdown
    original_install = generation._install_replacement
    original_observe = core._observe_core_task
    replacement_record: list[tuple[object, tuple[threading.Thread, ...]]] = []
    observer_entries: list[object] = []

    def hold_actual_drain(*args, **kwargs) -> None:
        entered.set()
        if not release.wait(timeout=6):
            raise AssertionError("Real generation drain was never released by fixture observation")
        original_shutdown(*args, **kwargs)

    def install_after_actual_old_join(replacement) -> None:
        assert generation.joined.is_set()
        assert all(not thread.is_alive() for thread in old_threads)
        replacement_future = replacement.submit(lambda: "new actual worker")
        assert replacement_future.result(timeout=3) == "new actual worker"
        new_threads = tuple(replacement._threads)
        assert new_threads and all(thread.ident is not None for thread in new_threads)
        replacement_record.append((replacement, new_threads))
        original_install(replacement)

    async def observe_and_release(task, originals):
        if task is generation.observer:
            assert entered.is_set()
            assert not task.done()
            assert generation.drain_thread is not None
            assert generation.drain_thread.ident is not None and generation.drain_thread.is_alive()
            assert async_workers._SHARED_EXECUTOR is shared
            observer_entries.append(task)
            release.set()
        return await original_observe(task, originals)

    monkeypatch.setattr(shared, "shutdown", hold_actual_drain)
    monkeypatch.setattr(generation, "_install_replacement", install_after_actual_old_join)
    monkeypatch.setattr(core, "_observe_core_task", observe_and_release)
    SCENARIOS["held_generation"] = (
        fixture.app,
        generation,
        shared,
        old_threads,
        entered,
        release,
        replacement_record,
        observer_entries,
    )
    generation.quarantine()
    deadline = asyncio.get_running_loop().time() + 3
    while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert entered.is_set()
    assert generation.drain_thread is not None and generation.drain_thread.is_alive()
    assert generation.observer is not None and not generation.observer.done()
    assert not generation.joined.is_set()
    # The f25 fixture must enter its observer before release. Its own final
    # shared shutdown then joins the installed replacement's actual thread.


@pytest.mark.asyncio
async def test_held_recovery_escalation_observed_before_shutdown(tmp_path, monkeypatch) -> None:
    """A pending real escalation is released only by f25 observation."""
    fixture, _registry = await core._core(tmp_path)
    recovery = fixture.app.state.process_recovery
    begin_recovery = recovery.watchdog.begin_recovery
    original_observe = core._observe_core_task
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[object] = []

    async def held_begin(reason):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=6)
        return await begin_recovery(reason)

    async def observe_and_release(task, originals):
        if task is recovery._escalation_task:
            assert entered.is_set() and not release.is_set()
            assert not task.done()
            observed.append(task)
            release.set()
        return await original_observe(task, originals)

    monkeypatch.setattr(recovery.watchdog, "begin_recovery", held_begin)
    monkeypatch.setattr(core, "_observe_core_task", observe_and_release)
    recovery.begin_shutdown()
    await asyncio.wait_for(entered.wait(), timeout=3)
    assert recovery._escalation_task is not None and not recovery._escalation_task.done()
    SCENARIOS["held_escalation"] = (fixture.app, recovery, entered, release, observed)
