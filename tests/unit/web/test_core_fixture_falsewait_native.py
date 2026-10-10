"""Unrun additive native cases for the reviewed 43ed core fixture.

Every producer is a real application Task or shared worker. The finite external
owner selects exactly one node per child; the child test never fabricates a
Task result or claims physical completion from a timeout.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from elspeth.web import async_workers
from tests.unit.web import test_execute_lease_cleanup_core as core

_owned_core_lifecycle = core._owned_core_lifecycle
SCENARIOS: dict[str, object] = {}


@pytest.fixture(scope="module", autouse=True)
def _requires_owned_falsewait_probe(request):
    """Run deliberate-red cases only under their explicit finite owner."""
    if not request.config.pluginmanager.hasplugin("core_falsewait_probe_plugin"):
        pytest.skip("core false-wait diagnostic requires its owned finite probe")


def _fail_one_wait(monkeypatch, selected, original):
    actual_wait = asyncio.wait
    hits = []

    async def fail_selected(fs, *args, **kwargs):
        if len(fs) == 1 and next(iter(fs)) is selected() and not hits:
            task = selected()
            assert task is not None and not task.done()
            hits.append(task)
            raise original
        return await actual_wait(fs, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", fail_selected)
    return hits


@pytest.mark.asyncio
async def test_observer_false_wait_joins_without_replacement(tmp_path, monkeypatch):
    fixture, _registry = await core._core(tmp_path)
    recovery = fixture.app.state.process_recovery
    shared = async_workers._get_shared_executor()
    generation = async_workers._generation_for(shared)
    future = shared.submit(lambda: "real old worker")
    assert future.result(timeout=3) == "real old worker"
    old_threads = tuple(shared._threads)
    assert old_threads and all(thread.ident is not None for thread in old_threads)
    entered, release = threading.Event(), threading.Event()
    actual_shutdown = shared.shutdown
    actual_join = core._join_known_core_task
    actual_install = generation._install_replacement
    wait_original = RuntimeError("exact core observer wait original")
    replacement_calls: list[object] = []
    joined_entries = []

    def held_shutdown(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=6):
            raise AssertionError("actual old drain release timed out")
        return actual_shutdown(*args, **kwargs)

    def record_replacement(replacement):
        replacement_calls.append(replacement)
        return actual_install(replacement)

    async def release_at_fallback(task, originals):
        if task is generation.observer:
            assert entered.is_set() and not release.is_set() and not task.done()
            assert any(error is wait_original for error in originals)
            joined_entries.append((task, originals))
            release.set()
        return await actual_join(task, originals)

    monkeypatch.setattr(shared, "shutdown", held_shutdown)
    monkeypatch.setattr(generation, "_install_replacement", record_replacement)
    monkeypatch.setattr(core, "_join_known_core_task", release_at_fallback)
    hits = _fail_one_wait(monkeypatch, lambda: generation.observer, wait_original)
    recovery.begin_shutdown()
    generation.quarantine()
    deadline = asyncio.get_running_loop().time() + 3
    while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert entered.is_set()
    assert generation.drain_thread is not None and generation.drain_thread.is_alive()
    assert generation.observer is not None and not generation.observer.done()
    SCENARIOS["observer"] = (
        fixture.app,
        generation,
        shared,
        old_threads,
        future,
        entered,
        release,
        hits,
        joined_entries,
        replacement_calls,
        wait_original,
    )


@pytest.mark.asyncio
async def test_escalation_false_wait_joins_exact_task(tmp_path, monkeypatch):
    fixture, _registry = await core._core(tmp_path)
    recovery = fixture.app.state.process_recovery
    shared = async_workers._get_shared_executor()
    future = shared.submit(lambda: "real escalation owner worker")
    assert future.result(timeout=3) == "real escalation owner worker"
    threads = tuple(shared._threads)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_begin = recovery.watchdog.begin_recovery
    actual_join = core._join_known_core_task
    joined_entries = []
    wait_original = RuntimeError("exact core escalation wait original")

    async def held_begin(reason):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=6)
        return await actual_begin(reason)

    async def release_at_fallback(task, originals):
        if task is recovery._escalation_task:
            assert entered.is_set() and not release.is_set() and not task.done()
            assert any(error is wait_original for error in originals)
            joined_entries.append((task, originals))
            release.set()
        return await actual_join(task, originals)

    monkeypatch.setattr(recovery.watchdog, "begin_recovery", held_begin)
    monkeypatch.setattr(core, "_join_known_core_task", release_at_fallback)
    hits = _fail_one_wait(monkeypatch, lambda: recovery._escalation_task, wait_original)
    recovery.begin_shutdown()
    await asyncio.wait_for(entered.wait(), timeout=3)
    assert recovery._escalation_task is not None and not recovery._escalation_task.done()
    SCENARIOS["escalation"] = (fixture.app, recovery, shared, threads, future, entered, release, hits, joined_entries, wait_original)


@pytest.mark.asyncio
async def test_shutdown_false_wait_retains_caller_cancellations(tmp_path, monkeypatch):
    fixture, _registry = await core._core(tmp_path)
    shared = async_workers._get_shared_executor()
    entered, release = threading.Event(), threading.Event()

    def actual_worker():
        entered.set()
        if not release.wait(timeout=6):
            raise AssertionError("actual shared worker release timed out")
        return "real held worker"

    future = shared.submit(actual_worker)
    assert entered.wait(timeout=3)
    threads = tuple(shared._threads)
    actual_join = core._join_known_core_task
    actual_shield = asyncio.shield
    wait_original = RuntimeError("exact core shutdown wait original")
    selected: dict[str, object] = {}
    joined_entries = []
    cancellation_labels = tuple(f"core fixture caller cancellation {index}" for index in range(3))
    injected: list[str] = []
    completion_boundary: list[object] = []

    def shield_with_caller_cancellation(task):
        if task is selected.get("task"):
            caller = asyncio.current_task()
            assert caller is not None
            if len(injected) < 3:
                label = cancellation_labels[len(injected)]
                injected.append(label)
                asyncio.get_running_loop().call_soon(caller.cancel, label)
            elif not release.is_set():
                release.set()

                async def forward_actual_completion():
                    result = await actual_shield(task)
                    assert task.done() and not task.cancelled()
                    completion_boundary.append(task)
                    # The helper is still awaiting this forwarding boundary.
                    # Deliver cancellation only after its exact producer is
                    # done, then propagate that object into the real helper.
                    caller = asyncio.current_task()
                    assert caller is not None
                    caller.cancel("core fixture postdone cancellation")
                    await asyncio.sleep(0)
                    return result

                return forward_actual_completion()
        return actual_shield(task)

    async def observe_fallback(task, originals):
        if task is selected.get("task"):
            assert not task.done() and any(error is wait_original for error in originals)
            joined_entries.append((task, originals))
            await actual_join(task, originals)
            assert task.done() and not task.cancelled()
            return
        return await actual_join(task, originals)

    actual_wait = asyncio.wait
    hits = []

    async def fail_shutdown_wait(fs, *args, **kwargs):
        if len(fs) == 1 and not hits:
            task = next(iter(fs))
            if task.get_name() == "core-shared-worker-shutdown":
                assert not task.done() and selected.get("task") is None
                selected["task"] = task
                hits.append(task)
                raise wait_original
        return await actual_wait(fs, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", fail_shutdown_wait)
    monkeypatch.setattr(asyncio, "shield", shield_with_caller_cancellation)
    monkeypatch.setattr(core, "_join_known_core_task", observe_fallback)
    SCENARIOS["shutdown"] = (
        fixture.app,
        shared,
        threads,
        future,
        entered,
        release,
        selected,
        hits,
        joined_entries,
        injected,
        completion_boundary,
        cancellation_labels,
        wait_original,
    )
