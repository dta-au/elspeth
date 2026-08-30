"""Bounded, redacted dependency readiness probes for the web service."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import stat
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
from sqlalchemy import Engine, create_engine, text

from elspeth.web.config import WebSettings
from elspeth.web.deployment_contract import resolve_deployment_state_mode
from elspeth.web.paths import managed_blob_directory
from elspeth.web.schema_probe import (
    SchemaState,
    postgres_engine_kwargs,
    probe_landscape_schema,
    probe_session_schema,
)
from elspeth.web.sessions.engine import create_session_engine

ReadinessProbeLabel = Literal["session", "landscape", "data_dir", "payload_store", "blob_dir"]
READINESS_CHECK_NAMES: tuple[str, ...] = (
    "auth_mode",
    "session_db",
    "session_schema",
    "landscape_db",
    "landscape_schema",
    "data_dir",
    "payload_store",
    "blob_dir",
)
_PROBE_SENTINEL = b"elspeth-readiness-probe"
_slog = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: tuple[ReadinessCheck, ...]


_ProbeResult = tuple[ReadinessCheck, ...]
_SourceFuture = concurrent.futures.Future[_ProbeResult]
_WrappedRegistration = tuple[_SourceFuture, asyncio.Future[_ProbeResult], asyncio.AbstractEventLoop]


def _static_failures(check_names: tuple[str, ...], detail: str) -> _ProbeResult:
    return tuple(ReadinessCheck(name=name, ok=False, detail=detail) for name in check_names)


def _exception_failures(check_names: tuple[str, ...], exc: BaseException) -> _ProbeResult:
    exc_class = type(exc).__name__
    db_name = next((name for name in check_names if name.endswith("_db")), None)
    if db_name is None:
        return _static_failures(check_names, f"probe failed ({exc_class})")
    schema_name = f"{db_name.removesuffix('_db')}_schema"
    return tuple(
        ReadinessCheck(
            name=name,
            ok=False,
            detail=(
                f"probe failed ({exc_class})"
                if name == db_name
                else "not checked: connectivity probe failed"
                if name == schema_name
                else f"probe failed ({exc_class})"
            ),
        )
        for name in check_names
    )


def _drain_source(future: _SourceFuture) -> None:
    """Mark a completed worker future's exception observed (never raises).

    Failure CONVERSION happens at the retrieval site in
    ``ReadinessProbeRunner.run`` (``_exception_failures``); this done-callback
    drain only retrieves the stored exception so it is not reported as
    never-observed. ``exception()`` on a done, non-cancelled
    concurrent.futures.Future returns the stored exception rather than
    raising, so no suppression is needed — the guards make retrieval total.
    """
    if future.cancelled() or not future.done():
        return
    future.exception()


def _drain_wrapped(future: asyncio.Future[_ProbeResult]) -> None:
    """Mark a completed wrapper future's exception observed (never raises).

    Same contract as :func:`_drain_source`; the not-done guard covers the
    direct timeout/cancellation call sites in ``run`` where ``cancel()`` did
    not take effect because the wrapper had already completed.
    """
    if future.cancelled() or not future.done():
        return
    future.exception()


def _cancel_wrapped_on_loop(
    wrapped: asyncio.Future[_ProbeResult],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Thread-safely cancel and drain a loop-owned wrapper."""

    def cancel_and_drain() -> None:
        wrapped.cancel()
        _drain_wrapped(wrapped)

    if loop.is_closed():
        return
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(cancel_and_drain)


def _schema_checks(kind: Literal["session", "landscape"], state: SchemaState) -> _ProbeResult:
    return (
        ReadinessCheck(f"{kind}_db", True, "connected"),
        ReadinessCheck(f"{kind}_schema", state is SchemaState.CURRENT, f"schema state: {state.name}"),
    )


def _probe_database_engine(
    engine: Engine,
    *,
    kind: Literal["session", "landscape"],
) -> _ProbeResult:
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET LOCAL statement_timeout = '1000ms'"))
        conn.execute(text("SELECT 1"))
        state = probe_session_schema(conn) if kind == "session" else probe_landscape_schema(conn)
    return _schema_checks(kind, state)


def _readiness_engine_kwargs(url: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(postgres_engine_kwargs(url))
    if not kwargs:
        return {}
    kwargs.update(
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
        connect_args={"connect_timeout": 1},
    )
    return kwargs


def _owned_database_check(
    url: str,
    *,
    kind: Literal["session", "landscape"],
) -> _ProbeResult:
    engine: Engine | None = None
    try:
        kwargs = _readiness_engine_kwargs(url)
        engine = create_session_engine(url, **kwargs) if kind == "session" else create_engine(url, **kwargs)
        return _probe_database_engine(engine, kind=kind)
    finally:
        if engine is not None:
            engine.dispose()


def _is_in_memory_sqlite(engine: Engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    database = engine.url.database
    return database == ":memory:" or str(engine.url) in {"sqlite:///:memory:", "sqlite://"}


def _check_session_database(
    settings: WebSettings,
    session_engine: Engine,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> _ProbeResult:
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    if state_mode == "external-postgresql":
        assert settings.session_db_url is not None
        return _owned_database_check(settings.session_db_url, kind="session")
    if _is_in_memory_sqlite(session_engine):
        remedy = "in-memory SQLite is not readiness-probeable; use a file-backed session database"
        return (
            ReadinessCheck("session_db", False, remedy),
            ReadinessCheck("session_schema", False, remedy),
        )
    if session_engine.dialect.name == "sqlite":
        return _probe_database_engine(session_engine, kind="session")
    return _owned_database_check(settings.get_session_db_url(), kind="session")


def _check_landscape_database(
    settings: WebSettings,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> _ProbeResult:
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    if state_mode == "external-postgresql":
        assert settings.landscape_url is not None
        url = settings.landscape_url
    else:
        url = settings.get_landscape_url()
    return _owned_database_check(url, kind="landscape")


def _probe_directory(name: str, directory: Path) -> _ProbeResult:
    # Each failed step records the declared failure ReadinessCheck; a cleanup
    # failure is appended after a probe failure and takes precedence (last
    # record wins below). Process-control exceptions (KeyboardInterrupt,
    # CancelledError) propagate: the worker future stores them and run()
    # converts at retrieval — a readiness check must not eat them.
    fd: int | None = None
    probe_name: str | None = None
    unlinked = False
    failures: list[ReadinessCheck] = []
    try:
        fd, probe_name = tempfile.mkstemp(prefix=".readiness-probe-", dir=directory)
        os.unlink(probe_name)
        unlinked = True
        with os.fdopen(fd, "w+b") as probe:
            fd = None
            probe.write(_PROBE_SENTINEL)
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            if probe.read() != _PROBE_SENTINEL:
                raise OSError("readiness probe readback did not match")
    except Exception as exc:
        failures.append(ReadinessCheck(name, False, f"directory probe failed ({type(exc).__name__})"))
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError as exc:
                failures.append(ReadinessCheck(name, False, f"directory probe cleanup failed ({type(exc).__name__})"))
        if probe_name is not None and not unlinked:
            try:
                Path(probe_name).unlink(missing_ok=True)
            except OSError as exc:
                failures.append(ReadinessCheck(name, False, f"directory probe cleanup failed ({type(exc).__name__})"))

    if failures:
        return (failures[-1],)
    return (ReadinessCheck(name, True, "directory is writable"),)


def _validate_directory(name: str, candidate: Path) -> _ProbeResult:
    try:
        candidate_stat = candidate.lstat()
        if stat.S_ISLNK(candidate_stat.st_mode):
            return (ReadinessCheck(name, False, f"{name} directory must not be a symlink"),)
        if not stat.S_ISDIR(candidate_stat.st_mode):
            return (ReadinessCheck(name, False, "directory is required and must already exist"),)
        if candidate_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return (ReadinessCheck(name, False, f"{name} group/world-writable directory is not allowed"),)
        directory = candidate.resolve(strict=True)
    except BaseException as exc:
        return (ReadinessCheck(name, False, f"directory validation failed ({type(exc).__name__})"),)
    return _probe_directory(name, directory)


def _check_data_dir(settings: WebSettings) -> _ProbeResult:
    return _validate_directory("data_dir", Path(settings.data_dir))


def _check_payload_store(
    settings: WebSettings,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> _ProbeResult:
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    if state_mode == "external-postgresql":
        assert settings.payload_store_path is not None
        path = Path(settings.payload_store_path)
    else:
        path = settings.get_payload_store_path()
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            return (ReadinessCheck("payload_store", False, "payload_store directory must not be a symlink"),)
        if not stat.S_ISDIR(path_stat.st_mode):
            return (ReadinessCheck("payload_store", False, "payload_store path must be an existing directory"),)
        if path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return (ReadinessCheck("payload_store", False, "payload_store group/world-writable directory is not allowed"),)
        directory = path.resolve(strict=True)
    except BaseException as exc:
        return (ReadinessCheck("payload_store", False, f"directory validation failed ({type(exc).__name__})"),)
    return _probe_directory("payload_store", directory)


def _check_blob_dir(settings: WebSettings) -> _ProbeResult:
    try:
        path = managed_blob_directory(str(settings.data_dir))
    except BaseException as exc:
        return (ReadinessCheck("blob_dir", False, f"directory resolution failed ({type(exc).__name__})"),)
    return _validate_directory("blob_dir", path)


def _check_auth_mode(settings: WebSettings) -> ReadinessCheck:
    # Treat the runtime value as open even though validated settings narrow it
    # statically; readiness is a total boundary if state is corrupted/mocked.
    provider = str(settings.auth_provider)
    if provider == "local":
        return ReadinessCheck("auth_mode", True, "local authentication configured")
    if provider == "oidc":
        ok = all((settings.oidc_issuer, settings.oidc_audience, settings.oidc_client_id))
        return ReadinessCheck("auth_mode", ok, "OIDC authentication configured" if ok else "OIDC configuration incomplete")
    if provider == "entra":
        ok = all((settings.entra_tenant_id, settings.oidc_audience, settings.oidc_client_id))
        return ReadinessCheck("auth_mode", ok, "Entra authentication configured" if ok else "Entra configuration incomplete")
    return ReadinessCheck("auth_mode", False, "unsupported authentication provider")


def _finalize(checks: tuple[ReadinessCheck, ...]) -> ReadinessReport:
    report = ReadinessReport(ready=all(check.ok for check in checks), checks=checks)
    for check in checks:
        if not check.ok:
            _slog.warning("readiness_check_not_ready", check=check.name, detail=check.detail)
    return report


def overall_timeout_report() -> ReadinessReport:
    return _finalize(_static_failures(READINESS_CHECK_NAMES, "readiness request timed out"))


async def readiness_report(
    settings: WebSettings,
    session_engine: Engine,
    runner: ReadinessProbeRunner,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> ReadinessReport:
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    tasks: list[asyncio.Task[_ProbeResult]] = []
    try:
        async with asyncio.timeout(5.0):
            tasks = [
                asyncio.create_task(
                    runner.run(
                        "session",
                        ("session_db", "session_schema"),
                        _check_session_database,
                        settings,
                        session_engine,
                        state_mode,
                    )
                ),
                asyncio.create_task(
                    runner.run(
                        "landscape",
                        ("landscape_db", "landscape_schema"),
                        _check_landscape_database,
                        settings,
                        state_mode,
                    )
                ),
                asyncio.create_task(runner.run("data_dir", ("data_dir",), _check_data_dir, settings)),
                asyncio.create_task(
                    runner.run(
                        "payload_store",
                        ("payload_store",),
                        _check_payload_store,
                        settings,
                        state_mode,
                    )
                ),
                asyncio.create_task(runner.run("blob_dir", ("blob_dir",), _check_blob_dir, settings)),
            ]
            groups = await asyncio.gather(*tasks)
        by_name = {check.name: check for group in groups for check in group}
        by_name["auth_mode"] = _check_auth_mode(settings)
        # Every runner.run() returns a complete named tuple for its labels
        # (timeout and exception paths included), so a missing name is a
        # first-party contract breach: the KeyError propagates into the
        # except BaseException arm below and becomes the fail-closed
        # "readiness evaluation failed (KeyError)" report instead of a
        # fabricated synthetic check.
        checks = tuple(by_name[name] for name in READINESS_CHECK_NAMES)
        return _finalize(checks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    except TimeoutError:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return overall_timeout_report()
    except BaseException as exc:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return _finalize(_static_failures(READINESS_CHECK_NAMES, f"readiness evaluation failed ({type(exc).__name__})"))


class ReadinessProbeRunner:
    """Admit at most one unresolved worker future for each closed label."""

    def __init__(self) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="readiness-worker",
        )
        self._lock = threading.Lock()
        self._futures: dict[ReadinessProbeLabel, _SourceFuture] = {}
        self._wrappers: dict[ReadinessProbeLabel, _WrappedRegistration] = {}
        self._closed = False

    async def run(
        self,
        label: ReadinessProbeLabel,
        check_names: tuple[str, ...],
        fn: Callable[..., _ProbeResult],
        *args: object,
    ) -> _ProbeResult:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                return _static_failures(check_names, "probe runner closed")
            existing = self._futures.get(label)
            if existing is not None and not existing.done():
                return _static_failures(check_names, "probe already in flight")
            source = self._executor.submit(fn, *args)
            wrapped = asyncio.wrap_future(source, loop=loop)
            self._futures[label] = source
            self._wrappers[label] = (source, wrapped, loop)

        def source_done(completed: _SourceFuture) -> None:
            with self._lock:
                if self._futures.get(label) is completed:
                    del self._futures[label]
            _drain_source(completed)

        def wrapped_done(completed: asyncio.Future[_ProbeResult]) -> None:
            with self._lock:
                # Absence is a legal state: a stale callback can run after a
                # newer registration for the same label was already cleaned
                # up. The identity check below makes the same decision for a
                # mismatched live registration; only OUR OWN registration is
                # deleted.
                registration = self._wrappers[label] if label in self._wrappers else None
                if registration is not None and registration[0] is source and registration[1] is completed:
                    del self._wrappers[label]
            _drain_wrapped(completed)

        # A Future that completed synchronously calls this callback inline.
        # Registration therefore must remain outside the registry lock.
        source.add_done_callback(source_done)
        wrapped.add_done_callback(wrapped_done)
        deadline = loop.time() + 2.0
        try:
            while not wrapped.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    wrapped.cancel()
                    source.cancel()
                    _drain_wrapped(wrapped)
                    return _static_failures(check_names, "probe timed out")
                await asyncio.wait({wrapped}, timeout=min(0.1, remaining))
            try:
                return wrapped.result()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return _exception_failures(check_names, exc)
        except asyncio.CancelledError:
            wrapped.cancel()
            source.cancel()
            _drain_wrapped(wrapped)
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(self._futures.values())
            wrappers = tuple(self._wrappers.values())
        for _source, wrapped, loop in wrappers:
            _cancel_wrapped_on_loop(wrapped, loop)
        for future in futures:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class _HarvestOutcome:
    """Explicit terminal state of a retired readiness compute task."""

    report: ReadinessReport | None = None
    cancelled: bool = False
    failure_class: str | None = None


class ReadinessCache:
    """Cancellation-safe single-flight cache with a two-second success TTL."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._report: ReadinessReport | None = None
        self._completed_at: float | None = None
        self._task: asyncio.Task[ReadinessReport] | None = None
        self._task_completed_at: float | None = None

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _record_completion(self, task: asyncio.Task[ReadinessReport]) -> None:
        if self._task is task:
            self._task_completed_at = self._now()

    def _harvest_locked(self, task: asyncio.Task[ReadinessReport]) -> _HarvestOutcome:
        """Retire a done compute task, reporting its terminal state explicitly.

        A failed task's exception class is returned in the outcome record so
        the waiterless-harvest path can log it (previously the failure was
        consumed with nothing recorded anywhere). A cancelled task retires
        quietly. A stored non-Exception BaseException (process control)
        propagates to the caller.
        """
        try:
            report = task.result()
        except asyncio.CancelledError:
            self._clear_if_current(task)
            return _HarvestOutcome(cancelled=True)
        except Exception as exc:
            self._clear_if_current(task)
            return _HarvestOutcome(failure_class=type(exc).__name__)
        if self._task is task:
            self._report = report
            self._completed_at = self._task_completed_at if self._task_completed_at is not None else self._now()
            self._task = None
            self._task_completed_at = None
        return _HarvestOutcome(report=report)

    def _clear_if_current(self, task: asyncio.Task[ReadinessReport]) -> None:
        if self._task is task:
            self._task = None
            self._task_completed_at = None

    async def get_or_compute(self, compute: Callable[[], Awaitable[ReadinessReport]]) -> ReadinessReport:
        async with self._lock:
            if self._task is not None and self._task.done():
                stale = self._harvest_locked(self._task)
                if stale.failure_class is not None:
                    # Waiterless failure: every original waiter was cancelled
                    # before this task finished. Record it — the next compute
                    # starts fresh, but the failure must not vanish.
                    _slog.warning("readiness_compute_failed_without_waiter", exc_class=stale.failure_class)

            now = self._now()
            if self._report is not None and self._completed_at is not None and now - self._completed_at < 2.0:
                return self._report

            if self._task is None:

                async def invoke() -> ReadinessReport:
                    return await compute()

                self._task = asyncio.create_task(invoke())
                self._task_completed_at = None
                self._task.add_done_callback(self._record_completion)
            task = self._task

        try:
            report = await asyncio.shield(task)
        except BaseException:
            # The awaited failure re-raises below for this waiter; the
            # harvest only retires cache state for a done task.
            if task.done():
                async with self._lock:
                    if self._task is task:
                        self._harvest_locked(task)
            raise

        async with self._lock:
            if self._task is task:
                harvested = self._harvest_locked(task)
                if harvested.report is not None:
                    report = harvested.report
        return report
