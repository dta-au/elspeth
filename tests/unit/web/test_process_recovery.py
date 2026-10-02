"""Required background workers must stop the real ASGI host, preserving teardown."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select

from elspeth.web import async_workers
from elspeth.web.app import create_app, lifespan
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceMembershipLost,
    web_instance_identity_from_settings,
)
from elspeth.web.coordination.membership_lifecycle import RegisteredWebInstanceMembership
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.operator_telemetry import OperatorTelemetryRuntime
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.models import web_instances_table
from elspeth.web.sessions.protocol import WebInstanceRecord
from tests.unit.web.test_app import _settings


def _run_host() -> None:
    """Child-only fault injection around the production application and lifespan."""
    root, mode, port_text = sys.argv[1:]
    directory = Path(root)
    trigger = directory / "fail"
    settings = _settings(directory, composer_boot_probe_enabled=False)
    app = create_app(settings)
    if mode == "membership":
        membership = RegisteredWebInstanceMembership(
            RepositoryWebInstanceMembershipAuthority(app.state.session_engine),
            web_instance_identity_from_settings(settings, instance_id="recovery-test"),
            lease_seconds=30,
            interval_seconds=1,
            process_recovery=app.state.process_recovery,
        )
        app.state.web_instance_membership = membership
        app.state.instance_draining = membership.draining

    original_heartbeat = RepositoryWebInstanceMembershipAuthority.heartbeat

    def heartbeat(authority: RepositoryWebInstanceMembershipAuthority, instance_id: str, *, lease_seconds: int) -> WebInstanceRecord:
        if trigger.exists():
            raise WebInstanceMembershipLost()
        return original_heartbeat(authority, instance_id, lease_seconds=lease_seconds)

    async def orphan_cleanup(*_args: object, **_kwargs: object) -> None:
        while not trigger.exists():
            await asyncio.sleep(0.01)
        raise OSError("injected fatal orphan failure")

    shutdown_steps: list[str] = []
    original_execution_shutdown = ExecutionServiceImpl.shutdown
    original_telemetry_shutdown = OperatorTelemetryRuntime.shutdown
    original_workers_shutdown = async_workers.shutdown_async_workers
    original_membership_stop = RegisteredWebInstanceMembership.stop

    async def execution_shutdown(service: ExecutionServiceImpl) -> None:
        await original_execution_shutdown(service)
        shutdown_steps.append("execution")

    async def telemetry_shutdown(telemetry: OperatorTelemetryRuntime) -> None:
        await original_telemetry_shutdown(telemetry)
        shutdown_steps.append("telemetry")

    async def workers_shutdown() -> None:
        await original_workers_shutdown()
        shutdown_steps.append("workers")

    async def membership_stop(membership: RegisteredWebInstanceMembership):
        try:
            return await original_membership_stop(membership)
        finally:
            with app.state.session_engine.connect() as connection:
                state = connection.execute(
                    select(web_instances_table.c.state).where(web_instances_table.c.instance_id == membership.identity.instance_id)
                ).scalar_one()
                assert state == "stopped"
            shutdown_steps.append("membership")

    @asynccontextmanager
    async def observed_lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            async with lifespan(application):
                yield
        finally:
            assert not application.state._session_engine_finalizer.alive
            shutdown_steps.append("engine")
            (directory / "shutdown").write_text(",".join(shutdown_steps), encoding="utf-8")

    app.router.lifespan_context = observed_lifespan
    with ExitStack() as patches:
        patches.enter_context(patch.object(RepositoryWebInstanceMembershipAuthority, "heartbeat", heartbeat))
        patches.enter_context(patch.object(ExecutionServiceImpl, "shutdown", execution_shutdown))
        patches.enter_context(patch.object(OperatorTelemetryRuntime, "shutdown", telemetry_shutdown))
        patches.enter_context(patch.object(async_workers, "shutdown_async_workers", workers_shutdown))
        patches.enter_context(patch.object(RegisteredWebInstanceMembership, "stop", membership_stop))
        if mode == "orphan":
            with patch("elspeth.web.app._periodic_orphan_cleanup", orphan_cleanup):
                uvicorn.run(app, host="127.0.0.1", port=int(port_text), log_level="error")
        else:
            uvicorn.run(app, host="127.0.0.1", port=int(port_text), log_level="error")


@pytest.mark.parametrize("mode", ["membership", "orphan"])
def test_fatal_worker_exits_uvicorn_process(tmp_path: Path, mode: str) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("ELSPETH_WEB__")}
    environment["LITELLM_MODE"] = "PRODUCTION"
    log = tmp_path / "host.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.unit.web.test_process_recovery import _run_host; _run_host()",
                str(tmp_path),
                mode,
                str(port),
            ],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    assert process.poll() is None, log.read_text(encoding="utf-8")
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/api/health")
                    except httpx.TransportError:
                        time.sleep(0.05)
                        continue
                    assert response.status_code == 200
                    break
                else:
                    pytest.fail(f"host did not become healthy: {log.read_text(encoding='utf-8')}")
                (tmp_path / "fail").touch()
                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    response = client.get(f"http://127.0.0.1:{port}/api/health")
                    pytest.fail(f"host survived fatal {mode} failure; health={response.status_code}; {log.read_text(encoding='utf-8')}")
            assert returncode == -signal.SIGTERM, log.read_text(encoding="utf-8")
            expected_shutdown = (
                "execution,membership,telemetry,workers,engine" if mode == "membership" else "execution,telemetry,workers,engine"
            )
            assert (tmp_path / "shutdown").read_text(encoding="utf-8") == expected_shutdown
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_repeated_worker_failures_send_one_host_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("elspeth.web.process_recovery.os.kill", lambda pid, sig: signals.append((pid, sig)))
    recovery = ProcessRecovery()
    recovery.request_shutdown()
    recovery.request_shutdown()
    recovery.begin_shutdown()
    recovery.request_shutdown()
    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_worker_failure_during_normal_shutdown_does_not_signal_again(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("elspeth.web.process_recovery.os.kill", lambda pid, sig: signals.append((pid, sig)))
    recovery = ProcessRecovery()
    recovery.begin_shutdown()
    recovery.request_shutdown()
    assert signals == []


@pytest.mark.asyncio
async def test_simultaneous_membership_and_orphan_failures_share_one_shutdown_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, composer_boot_probe_enabled=False)
    app = create_app(settings)
    membership = RegisteredWebInstanceMembership(
        RepositoryWebInstanceMembershipAuthority(app.state.session_engine),
        web_instance_identity_from_settings(settings, instance_id="simultaneous-failure"),
        lease_seconds=30,
        process_recovery=app.state.process_recovery,
    )
    app.state.web_instance_membership = membership
    app.state.instance_draining = membership.draining
    fail = asyncio.Event()
    signals: list[tuple[int, int]] = []
    failures: set[str] = set()

    async def heartbeat_failure(_membership: RegisteredWebInstanceMembership) -> None:
        await fail.wait()
        failures.add("membership")
        raise WebInstanceMembershipLost()

    async def orphan_failure(*_args: object, **_kwargs: object) -> None:
        await fail.wait()
        failures.add("orphan")
        raise OSError("injected simultaneous orphan failure")

    monkeypatch.setattr(RegisteredWebInstanceMembership, "_heartbeat_loop", heartbeat_failure)
    monkeypatch.setattr("elspeth.web.app._periodic_orphan_cleanup", orphan_failure)
    monkeypatch.setattr("elspeth.web.process_recovery.os.kill", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(WebInstanceMembershipLost):
        async with lifespan(app):
            fail.set()
            # One turn runs both failing workers; the next runs their callbacks.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert failures == {"membership", "orphan"}
            assert signals == [(os.getpid(), signal.SIGTERM)]
            assert membership.draining.is_set()
    assert signals == [(os.getpid(), signal.SIGTERM)]
