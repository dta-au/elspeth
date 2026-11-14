"""Precise faults around the actual standalone lifecycle and supervisor."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


async def embedded_calls(directory: Path, coordinator):
    from mcp.types import CallToolRequest, CallToolRequestParams

    from elspeth.composer_mcp.server import _build_stdio_server
    from elspeth.web.dependencies import create_catalog_service

    server = _build_stdio_server(
        create_catalog_service(), directory / "scratch", directory / "data", runtime_preflight_coordinator=coordinator
    )

    async def call(name, arguments):
        response = await server.request_handlers[CallToolRequest](
            CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
        )
        result = response.root
        return {"transport_error": result.content[0].text} if result.isError else json.loads(result.content[0].text)

    source = await call(
        "set_source",
        {"plugin": "null", "on_success": "result", "options": {"schema": {"mode": "observed"}}, "on_validation_failure": "discard"},
    )
    output = await call(
        "set_output",
        {
            "sink_name": "result",
            "plugin": "csv",
            "options": {"path": "output.csv", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
            "on_write_failure": "discard",
        },
    )
    assert source["success"] and output["success"]
    return await call("preview_pipeline", {})


def embedded_without_owner(directory: Path) -> None:
    import asyncio

    from elspeth.web import async_workers
    from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator

    async def run():
        assert async_workers._RECOVERY_CALLBACK is None
        coordinator = RuntimePreflightCoordinator()
        preview = await embedded_calls(directory, coordinator)
        assert preview == {"transport_error": "Tool error: AuditIntegrityError"}, preview
        assert async_workers._RECOVERY_CALLBACK is None and async_workers._SHARED_EXECUTOR is None
        await coordinator.seal_and_join()
        (directory / "embedded.json").write_text(json.dumps(preview))

    asyncio.run(run())


def child(directory: Path, mode: str) -> None:
    from elspeth.composer_mcp.runner import _authenticate_supervisor

    assert "elspeth.contracts" not in sys.modules
    assert "elspeth.composer_mcp.server" not in sys.modules
    nonce = os.environ["_ELSPETH_COMPOSER_CONTROL_NONCE"]
    connection = _authenticate_supervisor()
    assert "elspeth.contracts" not in sys.modules
    assert "elspeth.composer_mcp.server" not in sys.modules
    assert not any(name.startswith("elspeth.plugins") for name in sys.modules)
    assert "_ELSPETH_COMPOSER_CONTROL_NONCE" not in os.environ
    assert "_ELSPETH_COMPOSER_CONTROL_PORT" not in os.environ
    wire = sys.modules["elspeth.contracts.process_watchdog_wire"]

    import asyncio
    import subprocess
    import threading
    import time
    from contextlib import asynccontextmanager

    from elspeth.composer_mcp import server, standalone
    from elspeth.composer_mcp.portable_control import PortableProcessWatchdog
    from elspeth.contracts import process_watchdog_wire
    from elspeth.web import async_workers

    descendant = subprocess.check_output([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"])
    assert nonce.encode() not in descendant
    assert b"_ELSPETH_COMPOSER_CONTROL_" not in descendant

    assert wire is process_watchdog_wire
    assert wire.ProcessWatchdogTarget is process_watchdog_wire.ProcessWatchdogTarget
    assert wire.ProcessCompletionWitness is process_watchdog_wire.ProcessCompletionWitness
    (directory / "authenticated.json").write_text(
        json.dumps({"pid": os.getpid(), "canonical_wire": True, "bootstrap_consumed": True, "descendant_bootstrap_absent": True})
    )
    draining = threading.Event()
    watchdog = PortableProcessWatchdog(connection, draining)
    begin = watchdog.begin_recovery
    complete = watchdog.complete

    async def observed_begin(reason):
        receipt = await begin(reason)
        (directory / "draining.json").write_text(json.dumps({"admissions": async_workers.outstanding_admissions()}))
        return receipt

    async def observed_complete(witness):
        assert async_workers.outstanding_admissions() == 0
        assert not async_workers._RECOVERING_GENERATIONS
        assert async_workers._SHARED_EXECUTOR is None
        (directory / "completion").write_text("settled")
        await complete(witness)

    watchdog.begin_recovery = observed_begin
    watchdog.complete = observed_complete
    if mode.startswith("blocked"):
        validate = server.validate_pipeline

        def held_validation(*args, **kwargs):
            (directory / "worker").write_text("entered")
            while not (directory / "release").exists():
                time.sleep(0.01)
            return validate(*args, **kwargs)

        server.validate_pipeline = held_validation
    if mode == "quarantine":
        from concurrent.futures import ThreadPoolExecutor

        from elspeth.web.process_recovery import ProcessRecovery

        catalog_factory = standalone.create_catalog_service
        expired = ProcessRecovery.required_generation_expired

        def observed_expiry(recovery, observation):
            expired(recovery, observation)
            (directory / "expired").write_text("actual required generation expiry")

        ProcessRecovery.required_generation_expired = observed_expiry

        class QueueThenRaiseExecutor(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                super().submit(fn, *args, **kwargs)
                (directory / "worker").write_text("actual queued submission fault")
                raise RuntimeError("submit failed after queueing")

        def quarantining_catalog():
            catalog = catalog_factory()
            executor = QueueThenRaiseExecutor(max_workers=1)

            def physical_blocker():
                while not (directory / "release").exists():
                    time.sleep(0.01)

            ThreadPoolExecutor.submit(executor, physical_blocker)
            async_workers._SHARED_EXECUTOR = executor
            return catalog

        standalone.create_catalog_service = quarantining_catalog
    if mode == "startup-failure":

        def failed_catalog():
            raise LookupError("startup fault")

        standalone.create_catalog_service = failed_catalog
    if mode in ("transport-failure", "cleanup-failure"):
        transport = server.stdio_server

        @asynccontextmanager
        async def failing_transport():
            async with transport() as streams:
                yield streams
            raise OSError("transport fault")

        server.stdio_server = failing_transport
    if mode == "cleanup-failure":
        shutdown = standalone.shutdown_async_workers

        async def failed_shutdown():
            await shutdown()
            (directory / "workers-joined").write_text("settled")
            raise ArithmeticError("cleanup fault")

        standalone.shutdown_async_workers = failed_shutdown

    async def run():
        try:
            if mode == "embedded-owned":
                from elspeth.contracts.process_watchdog_wire import ProcessCompletionWitness
                from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
                from elspeth.web.process_recovery import ProcessRecovery

                root = asyncio.current_task()
                assert root is not None
                watchdog.bind_root_task(root)
                recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
                recovery.start_monitor()
                unavailable = threading.Event()
                callback = recovery.required_generation_expired
                async_workers.configure_required_executor_recovery(
                    drain_seconds=10, instance_draining=draining, generation_unavailable=unavailable, recovery_callback=callback
                )
                coordinator = RuntimePreflightCoordinator()
                preview = await embedded_calls(directory, coordinator)
                assert preview["data"]["preview_is_valid"], preview
                assert async_workers._RECOVERY_CALLBACK is callback
                assert async_workers._INSTANCE_DRAINING is draining and not draining.is_set()
                assert async_workers._GENERATION_UNAVAILABLE is unavailable
                assert async_workers._SHARED_EXECUTOR is not None
                assert not async_workers._SHARED_SHUTDOWN_STARTED and not coordinator._sealed
                (directory / "embedded.json").write_text(
                    json.dumps({"preview_valid": True, "host_owner_preserved": True, "host_coordinator_open": True})
                )
                recovery.begin_shutdown()
                await recovery.join_escalation()
                await coordinator.seal_and_join()
                await async_workers.shutdown_async_workers()
                await watchdog.complete(ProcessCompletionWitness(watchdog.target))
                await recovery.join_monitor_after_completion()
            else:
                await standalone.run_standalone(watchdog, draining, directory / "scratch", directory / "data")
        except BaseException as error:

            def classes(exc):
                if isinstance(exc, BaseExceptionGroup):
                    return [item for child_error in exc.exceptions for item in classes(child_error)]
                return [type(exc).__name__]

            (directory / "outcomes.json").write_text(json.dumps(classes(error)))
            raise

    asyncio.run(run())


if __name__ == "__main__":
    if sys.argv[2] == "embedded-unowned":
        embedded_without_owner(Path(sys.argv[1]))
    else:
        child(Path(sys.argv[1]), sys.argv[2])
