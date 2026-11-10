"""Actual standalone root, executor ownership and truthful completion."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

from elspeth.composer_mcp.portable_control import PortableProcessWatchdog
from elspeth.composer_mcp.server import run_server
from elspeth.contracts.process_watchdog_wire import ProcessCompletionWitness
from elspeth.web.async_workers import configure_standalone_executor_recovery, shutdown_async_workers
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
from elspeth.web.process_recovery import ProcessRecovery


async def run_standalone(watchdog: PortableProcessWatchdog, draining: threading.Event, scratch_dir: Path, data_dir: Path) -> None:
    """Own exactly this process's serving root; never adopt a host registration."""
    root = asyncio.current_task()
    assert root is not None
    watchdog.bind_root_task(root)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
    recovery.start_monitor()
    coordinator = RuntimePreflightCoordinator()
    owned = False
    originals: list[BaseException] = []
    cleanup_failed = False
    try:
        configure_standalone_executor_recovery(
            drain_seconds=10.0,
            instance_draining=draining,
            generation_unavailable=threading.Event(),
            recovery_callback=recovery.required_generation_expired,
        )
        owned = True
        catalog = create_catalog_service()
        await run_server(catalog, scratch_dir, data_dir, runtime_preflight_coordinator=coordinator)
    except BaseException as original:
        originals.append(original)
    finally:
        # The independent grace is armed before any possibly blocked join.
        recovery.begin_shutdown()

        async def settle(action: Callable[[], Awaitable[None]]) -> None:
            nonlocal cleanup_failed
            # Retain each action across repeated cancellation, then continue to
            # later actions. Cancellation is an outcome, never proof of a join.
            task = asyncio.ensure_future(action())
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as cancellation:
                    originals.append(cancellation)
                except BaseException:
                    break
            try:
                task.result()
            except BaseException as failure:
                cleanup_failed = True
                originals.append(failure)

        await settle(recovery.join_escalation)
        await settle(coordinator.seal_and_join)
        if owned:
            await settle(shutdown_async_workers)
        # A failed join cannot issue the nominal completion assertion. An
        # unresolved join remains awaited under the independent deadline.
        if owned and not cleanup_failed:
            await settle(lambda: watchdog.complete(ProcessCompletionWitness(watchdog.target)))
            if not cleanup_failed:
                await settle(recovery.join_monitor_after_completion)

    if len(originals) == 1:
        raise originals[0]
    if originals:
        raise BaseExceptionGroup("Composer serving and cleanup outcomes", originals)
