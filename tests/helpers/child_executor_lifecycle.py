"""Explicit shared-executor lifecycle for standalone spawned test processes."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.process_watchdog import ProcessCompletionWitness
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.fixtures.required_executor import assert_owned_executor_fixture_settled


class ChildExecutorLifecycle:
    def __init__(self, *, drain_seconds: float = 10.0) -> None:
        # A fresh spawned target has no pytest autouse lifecycle. Do not replace
        # another owner or reset an existing generation to make a fixture pass.
        if (
            async_workers._RECOVERY_CALLBACK is not None
            or async_workers._GENERATION_CUSTODIAN is not None
            or async_workers._SHARED_EXECUTOR is not None
            or async_workers.outstanding_admissions() != 0
        ):
            raise RuntimeError("Spawned test executor already has a lifecycle owner")
        self.instance_draining = threading.Event()
        self.generation_unavailable = threading.Event()
        self.watchdog = OwnedTestProcessWatchdog(self.instance_draining)
        self.process_recovery = ProcessRecovery(watchdog=self.watchdog, instance_draining=self.instance_draining)
        self.finalizer_owner = ApplicationFinalizerOwner()
        self.watchdog.assert_watching()
        async_workers.configure_required_executor_recovery(
            drain_seconds=drain_seconds,
            instance_draining=self.instance_draining,
            generation_unavailable=self.generation_unavailable,
            recovery_callback=self.process_recovery.required_generation_expired,
            application_finalizer_owner=self.finalizer_owner,
        )

    async def close(self, *, target_succeeded: bool) -> None:
        # Targets close their own DB resources before returning. Deliberate
        # os._exit and parent kill remain real deaths; they do not enter here.
        assert_owned_executor_fixture_settled(async_workers._GENERATION_CUSTODIAN)
        self.finalizer_owner.seal()
        self.process_recovery.begin_shutdown()
        await self.process_recovery.join_escalation()
        await async_workers.shutdown_async_workers()
        assert_owned_executor_fixture_settled(async_workers._GENERATION_CUSTODIAN)
        if target_succeeded:
            await self.watchdog.complete(ProcessCompletionWitness(self.watchdog.target))


@contextmanager
def child_executor_lifecycle(*, drain_seconds: float = 10.0) -> Iterator[ChildExecutorLifecycle]:
    owner = ChildExecutorLifecycle(drain_seconds=drain_seconds)
    primary: BaseException | None = None
    try:
        yield owner
    except BaseException as original:
        primary = original
    try:
        asyncio.run(owner.close(target_succeeded=primary is None))
    except BaseException as cleanup:
        if primary is not None and cleanup is not primary:
            raise BaseExceptionGroup("Child target and executor cleanup failed", [primary, cleanup]) from None
        raise
    if primary is not None:
        raise primary
