"""Factory ordering, cached readiness and closed finalizer controls."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerKind, ApplicationFinalizerOwner
from elspeth.web.process_watchdog_codec import RecoveryReason
from elspeth.web.readiness import ReadinessCheck, ReadinessReport
from elspeth.web.required_executor import RequiredGenerationUnavailable
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.fixtures.required_executor import RecordingRequiredGenerationRecovery
from tests.helpers.composer_operations import build_composer_operation_app


@pytest.mark.asyncio
@pytest.mark.parametrize("latch", ["instance_draining", "required_generation_unavailable"])
async def test_cached_ready_refuses_immediately_for_exact_local_latch(tmp_path, latch) -> None:
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app

    async def healthy():
        return ReadinessReport(True, (ReadinessCheck("controlled", True, "healthy"),))

    await app.state.readiness_cache.get_or_compute(healthy)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/ready")).status_code == 200
        event = app.state.instance_draining if latch == "instance_draining" else app.state.required_generation_unavailable
        event.set()
        response = await client.get("/api/ready")
        assert response.status_code == 503 and response.json()["ready"] is False
    assert app.state.web_instance_membership.draining is app.state.instance_draining
    assert app.state.process_recovery.instance_draining is app.state.instance_draining


@pytest.mark.asyncio
async def test_ready_rechecks_drain_after_successful_compute(tmp_path, monkeypatch) -> None:
    import elspeth.web.app as web_app

    fixture = await build_composer_operation_app(tmp_path)

    async def turn_draining(*args, **kwargs):
        fixture.app.state.instance_draining.set()
        return ReadinessReport(True, (ReadinessCheck("controlled", True, "healthy"),))

    monkeypatch.setattr(web_app, "readiness_report", turn_draining)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url="http://test") as client:
        response = await client.get("/api/ready")
        assert response.status_code == 503 and response.json()["ready"] is False


@pytest.mark.asyncio
async def test_constructor_fault_arms_watchdog_before_existing_side_effect_cleanup(tmp_path, monkeypatch) -> None:
    import elspeth.web.app as web_app
    from elspeth.web.config import WebSettings

    watchdogs = []

    def factory(event):
        watchdog = OwnedTestProcessWatchdog(event)
        watchdogs.append(watchdog)
        return watchdog

    def fail_telemetry(settings, *, cleanup_owner):
        from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner

        assert isinstance(cleanup_owner, OperatorTelemetryCleanupOwner)
        assert len(watchdogs) == 1
        assert not watchdogs[0].completed
        raise RuntimeError("controlled construction fault")

    monkeypatch.setattr(web_app, "bootstrap_operator_telemetry", fail_telemetry)
    with pytest.raises(RuntimeError, match="controlled construction fault"):
        web_app.create_app(
            WebSettings(
                data_dir=tmp_path,
                composer_boot_probe_enabled=False,
                composer_max_composition_turns=15,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=60,
                composer_rate_limit_per_minute=100,
                shareable_link_signing_key=b"\x00" * 32,
            ),
            process_watchdog_factory=factory,
        )
    assert watchdogs[0].reasons == [RecoveryReason.FAILED_STARTUP]
    assert watchdogs[0].draining.is_set() and not watchdogs[0].completed
    assert web_app._FAILED_BOOTSTRAP_RECOVERY is not None


@pytest.mark.asyncio
async def test_finalizer_capability_is_single_use_and_never_permits_foreign_or_quarantined_work() -> None:
    owner = ApplicationFinalizerOwner()
    other = ApplicationFinalizerOwner()
    calls = []
    capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: calls.append("owned"))
    refused_capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_DRAIN, lambda: calls.append("quarantined"))
    foreign = other.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: calls.append("foreign"))
    owner.seal()
    other.seal()
    draining = threading.Event()
    unavailable = threading.Event()
    async_workers.configure_required_executor_recovery(
        drain_seconds=10,
        instance_draining=draining,
        generation_unavailable=unavailable,
        recovery_callback=RecordingRequiredGenerationRecovery(),
        application_finalizer_owner=owner,
    )
    draining.set()
    with pytest.raises(RequiredGenerationUnavailable):
        await async_workers.run_sync_in_worker(lambda: calls.append("ordinary"))
    with pytest.raises(AuditIntegrityError, match="Foreign"):
        await async_workers.run_application_finalizer_in_worker(foreign)
    await async_workers.run_application_finalizer_in_worker(capability)
    assert calls == ["owned"]
    with pytest.raises(AuditIntegrityError, match="reused"):
        await async_workers.run_application_finalizer_in_worker(capability)
    unavailable.set()
    with pytest.raises(RequiredGenerationUnavailable):
        await async_workers.run_application_finalizer_in_worker(refused_capability)
    with pytest.raises(RequiredGenerationUnavailable):
        await async_workers.run_sync_in_worker(lambda: calls.append("quarantined"))
    assert calls == ["owned"]
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_actual_lifespan_disarms_only_after_joined_owned_shutdown(tmp_path, monkeypatch) -> None:
    import elspeth.web.app as web_app

    async def offline_catalog_prime(settings):
        return None

    monkeypatch.setattr(web_app, "_boot_prime_openrouter_catalog", offline_catalog_prime)
    fixture = await build_composer_operation_app(tmp_path)
    watchdog = fixture.app.state.process_watchdog
    assert isinstance(watchdog, OwnedTestProcessWatchdog)
    async with fixture.app.router.lifespan_context(fixture.app):
        assert not watchdog.completed
        assert fixture.app.state.process_recovery.instance_draining is watchdog.draining
    assert watchdog.completed
    assert watchdog.reasons == [RecoveryReason.NORMAL_SHUTDOWN]
    assert fixture.composer.calls == 0
    assert async_workers.outstanding_admissions() == 0
    assert async_workers._SHARED_EXECUTOR is None


def test_claimed_finalizer_callable_executes_once_even_when_invocation_fails() -> None:
    owner = ApplicationFinalizerOwner()
    calls = []

    def fail() -> None:
        calls.append("owned")
        raise ValueError("controlled finalizer fault")

    capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, fail)
    owner.seal()
    owner.claim(capability)
    with pytest.raises(ValueError, match="controlled finalizer fault"):
        capability.invoke()
    with pytest.raises(AuditIntegrityError, match="reused"):
        capability.invoke()
    assert calls == ["owned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_owner", [False, True])
async def test_finalizer_invalid_lifecycle_state_is_integrity_before_submission(monkeypatch, missing_owner) -> None:
    owner = ApplicationFinalizerOwner()
    calls = []
    capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: calls.append("forbidden"))
    owner.seal()
    monkeypatch.setattr(async_workers, "_APPLICATION_FINALIZER_OWNER", None if missing_owner else owner)
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", threading.Event())
    before = async_workers.outstanding_admissions()
    with pytest.raises(AuditIntegrityError):
        await async_workers.run_application_finalizer_in_worker(capability)
    assert not capability.claimed and calls == []
    assert async_workers.outstanding_admissions() == before


def test_unsealed_unclaimed_and_duplicate_finalizer_authority_remain_integrity() -> None:
    owner = ApplicationFinalizerOwner()
    capability = owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: pytest.fail("unclaimed finalizer ran"))
    with pytest.raises(AuditIntegrityError):
        capability.invoke()
    with pytest.raises(AuditIntegrityError):
        owner.claim(capability)
    with pytest.raises(AuditIntegrityError):
        owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: None)
    assert not capability.claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_begin_drain_failure_retains_original_and_joins_before_refusing_watchdog_completion(
    tmp_path, monkeypatch, late_failure
) -> None:
    import elspeth.web.app as web_app

    async def offline_catalog_prime(settings):
        return None

    monkeypatch.setattr(web_app, "_boot_prime_openrouter_catalog", offline_catalog_prime)
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app
    watchdog = app.state.process_watchdog
    assert isinstance(watchdog, OwnedTestProcessWatchdog)
    drain_failure = AuditIntegrityError("controlled drain failure")
    telemetry_failure = RuntimeError("controlled late cleanup failure")
    joins = []

    context = app.router.lifespan_context(app)
    await context.__aenter__()
    try:
        membership = app.state.web_instance_membership
        execution = app.state.execution_service
        original_execution_shutdown = execution.shutdown
        original_membership_stop = membership.stop
        original_telemetry_shutdown = app.state.operator_telemetry.shutdown

        async def fail_drain(self):
            assert self is membership
            raise drain_failure

        async def join_execution():
            await original_execution_shutdown()
            joins.append("execution")

        async def join_membership(self):
            assert self is membership
            await original_membership_stop()
            joins.append("membership")

        async def join_telemetry(self):
            assert self is app.state.operator_telemetry
            await original_telemetry_shutdown()
            joins.append("telemetry")
            if late_failure:
                raise telemetry_failure

        monkeypatch.setattr(type(membership), "begin_drain", fail_drain)
        monkeypatch.setattr(execution, "shutdown", join_execution)
        monkeypatch.setattr(type(membership), "stop", join_membership)
        monkeypatch.setattr(type(app.state.operator_telemetry), "shutdown", join_telemetry)
        with pytest.raises(BaseException) as raised:
            await context.__aexit__(None, None, None)
        roots = raised.value.exceptions if isinstance(raised.value, BaseExceptionGroup) else (raised.value,)
        assert drain_failure in roots
        if late_failure:
            assert telemetry_failure in roots
        assert joins == ["execution", "membership", "telemetry"]
        app.state.composer_async_worker.assert_shutdown_complete()
        assert async_workers.outstanding_admissions() == 0
        assert async_workers._SHARED_EXECUTOR is None
        assert not watchdog.completed and watchdog.draining.is_set()
        assert fixture.composer.calls == 0
    finally:
        monitor_failure = RuntimeError("controlled fake helper exit")
        watchdog.fail(monitor_failure)
        with pytest.raises(RuntimeError) as monitored:
            await app.state.process_recovery.join_monitor_after_completion()
        assert monitored.value is monitor_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["quarantine", "shutdown", "draining", "old_owner"])
async def test_actual_http_ordinary_generation_refusal_has_fixed_safe_body_and_no_callable(tmp_path, monkeypatch, closure) -> None:
    fixture = await build_composer_operation_app(tmp_path)
    calls = []

    @fixture.app.get("/owned-generation-control")
    async def generation_control():
        if closure == "old_owner":
            async_workers.configure_required_executor_recovery(
                drain_seconds=10,
                instance_draining=threading.Event(),
                generation_unavailable=threading.Event(),
                recovery_callback=RecordingRequiredGenerationRecovery(),
            )
        await async_workers.run_sync_in_worker(lambda: calls.append("forbidden"))
        return {"unexpected": True}

    # Mount the owned test control before the production SPA catch-all.
    fixture.app.router.routes.insert(0, fixture.app.router.routes.pop())

    # A healthy actual submission proves this harness can enter the callable.
    assert await async_workers.run_sync_in_worker(lambda: "healthy") == "healthy"
    if closure == "quarantine":
        async_workers._GENERATION_UNAVAILABLE.set()
    elif closure == "draining":
        async_workers._INSTANCE_DRAINING.set()
    else:
        monkeypatch.setattr(async_workers, "_SHARED_SHUTDOWN_STARTED", True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url="http://test") as client:
            response_task = asyncio.create_task(client.get("/owned-generation-control"))
            try:
                async with asyncio.timeout(5):
                    while not response_task.done():
                        await asyncio.sleep(0.01)
                response = response_task.result()
            finally:
                if not response_task.done():
                    response_task.cancel()
                    await asyncio.gather(response_task, return_exceptions=True)
        assert response.status_code == 503
        body = response.json()
        assert body["detail"] == "Database is currently unavailable. Please retry in a moment."
        assert body["error_type"] == "database_unavailable"
        assert isinstance(body["request_id"], str) and body["request_id"]
        assert set(body) == {"detail", "error_type", "request_id"}
        assert calls == [] and fixture.composer.calls == 0
        assert async_workers.outstanding_admissions() == 0
    finally:
        await async_workers.shutdown_async_workers()


def test_watching_only_bootstrap_fault_completes_without_arming_recovery(tmp_path, monkeypatch) -> None:
    import elspeth.web.app as web_app
    from elspeth.web.config import WebSettings

    watchdogs = []
    original = AuditIntegrityError("controlled pre-side-effect registration fault")

    def factory(event):
        watchdog = OwnedTestProcessWatchdog(event)
        watchdogs.append(watchdog)
        return watchdog

    def refuse_registration(**kwargs):
        raise original

    monkeypatch.setattr(web_app, "configure_required_executor_recovery", refuse_registration)
    with pytest.raises(AuditIntegrityError) as raised:
        web_app.create_app(
            WebSettings(
                data_dir=tmp_path,
                composer_boot_probe_enabled=False,
                composer_max_composition_turns=15,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=60,
                composer_rate_limit_per_minute=100,
                shareable_link_signing_key=b"\x00" * 32,
            ),
            process_watchdog_factory=factory,
        )
    assert raised.value is original
    assert len(watchdogs) == 1 and watchdogs[0].completed
    assert watchdogs[0].reasons == [] and not watchdogs[0].draining.is_set()
    assert web_app._FAILED_BOOTSTRAP_RECOVERY is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["blob_reconciliation", "membership_start", "execution_lock", "execution_constructor", "catalog_prime", "run_recovery", "worker_start"],
)
async def test_startup_acquisition_fault_arms_before_once_finalization_and_joins_returned_owners(tmp_path, monkeypatch, boundary) -> None:
    import elspeth.web.app as web_app
    import elspeth.web.execution.service as execution_module

    async def offline_catalog_prime(settings):
        return None

    monkeypatch.setattr(web_app, "_boot_prime_openrouter_catalog", offline_catalog_prime)
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app
    watchdog = app.state.process_watchdog
    assert isinstance(watchdog, OwnedTestProcessWatchdog)
    original = AuditIntegrityError("controlled acquisition failure")
    finalizations = []
    actual_engine_finalizer = web_app._run_session_engine_finalizer

    def observe_engine_finalizer(finalizer, *, primary_error=None):
        assert watchdog.reasons == [RecoveryReason.FAILED_STARTUP]
        finalizations.append("engine")
        return actual_engine_finalizer(finalizer, primary_error=primary_error)

    async def fail_async(*args, **kwargs):
        raise original

    def fail_sync(*args, **kwargs):
        raise original

    executor_attempts = []
    actual_executor = execution_module.ThreadPoolExecutor

    def observe_executor(*args, **kwargs):
        executor_attempts.append("constructor")
        if boundary == "execution_constructor":
            raise original
        return actual_executor(*args, **kwargs)

    monkeypatch.setattr(execution_module, "ThreadPoolExecutor", observe_executor)
    monkeypatch.setattr(web_app, "_run_session_engine_finalizer", observe_engine_finalizer)
    if boundary == "blob_reconciliation":
        monkeypatch.setattr(type(app.state.blob_service), "reconcile_inline_custody_publications", fail_async)
    elif boundary == "membership_start":
        monkeypatch.setattr(type(app.state.web_instance_membership), "start", fail_async)
    elif boundary == "execution_lock":
        monkeypatch.setattr(execution_module, "threading", SimpleNamespace(Lock=fail_sync, Event=threading.Event))
    elif boundary == "execution_constructor":
        pass
    elif boundary == "catalog_prime":
        monkeypatch.setattr(web_app, "_boot_prime_openrouter_catalog", fail_async)
    elif boundary == "run_recovery":
        monkeypatch.setattr(web_app.RunRecoveryCoordinator, "recover", fail_async)
    else:
        monkeypatch.setattr(web_app.ComposerAsyncWorker, "start", fail_sync)
    tasks_before = asyncio.all_tasks()
    context = app.router.lifespan_context(app)
    try:
        with pytest.raises(AuditIntegrityError) as raised:
            await context.__aenter__()
        assert raised.value is original
        assert finalizations == ["engine"]
        assert watchdog.draining.is_set() and not watchdog.completed
        if boundary == "execution_lock":
            assert executor_attempts == []
        elif boundary in {"execution_constructor", "catalog_prime", "run_recovery", "worker_start"}:
            assert executor_attempts == ["constructor"]
        app.state.composer_async_worker.assert_shutdown_complete()
        if boundary in {"catalog_prime", "run_recovery", "worker_start"}:
            assert app.state.execution_service._executor._shutdown
            assert all(not thread.is_alive() for thread in app.state.execution_service._executor._threads)
        assert async_workers._SHARED_EXECUTOR is None and async_workers.outstanding_admissions() == 0
        assert asyncio.all_tasks() - tasks_before == {app.state.process_recovery._monitor_task}
        assert fixture.composer.calls == 0
    finally:
        monitor_failure = RuntimeError("controlled fake helper exit")
        watchdog.fail(monitor_failure)
        with pytest.raises(RuntimeError) as observed:
            await app.state.process_recovery.join_monitor_after_completion()
        assert observed.value is monitor_failure
