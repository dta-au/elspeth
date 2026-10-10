"""Isolated real-watchdog control; never execute in the pytest parent process."""

from __future__ import annotations

import asyncio
import os
import threading

from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerKind, ApplicationFinalizerOwner
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.process_watchdog import create_process_watchdog
from elspeth.web.process_watchdog_codec import RecoveryReason


async def main() -> None:
    draining = threading.Event()
    watchdog = create_process_watchdog(draining)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
    owner = ApplicationFinalizerOwner()
    capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: None)
    owner.seal()
    async_workers.configure_required_executor_recovery(
        drain_seconds=10.0,
        instance_draining=draining,
        generation_unavailable=threading.Event(),
        recovery_callback=recovery.required_generation_expired,
        application_finalizer_owner=owner,
    )
    actual_begin_recovery = watchdog.begin_recovery
    actual_complete = watchdog.complete

    async def observe_ack(reason):
        acknowledgement = await actual_begin_recovery(reason)
        if reason is RecoveryReason.NORMAL_SHUTDOWN:
            os.write(1, b"ACTUAL_NORMAL_SHUTDOWN_RECOVERY_ACK\n")
        return acknowledgement

    async def observe_complete(witness):
        os.write(1, b"FORBIDDEN_WATCHDOG_COMPLETE\n")
        return await actual_complete(witness)

    watchdog.begin_recovery = observe_ack
    watchdog.complete = observe_complete
    counter_original = RuntimeError("actual counter failed before decrement")

    def fail_counter() -> None:
        raise counter_original

    async_workers._release_admission = fail_counter
    recovery.start_monitor()
    recovery.begin_shutdown()
    await recovery.join_escalation()
    joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        generation = async_workers._GENERATION_CUSTODIAN
        if generation is not None and generation.joined.is_set():
            break
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    assert capability._physical_future is not None and capability._physical_future.done()
    assert capability._physical_reservation is not None and capability._physical_reservation.released
    assert capability._physical_callback_failure_exit_observed
    assert any(original is counter_original for original in capability.physical_failure_originals)
    assert not capability._physical_admission_release_return_observed
    assert not capability.physical_failure_completion_known
    assert not capability.physical_completion_succeeded
    assert not joining.done()
    assert async_workers.outstanding_admissions() == 1
    os.write(1, b"ACTUAL_JOINED_GENERATION_FAILED_COUNTER_STAYS_UNKNOWN\n")
    await joining
    raise AssertionError("Unreleased actual admission allowed finalizer completion")


if __name__ == "__main__":
    asyncio.run(main())
