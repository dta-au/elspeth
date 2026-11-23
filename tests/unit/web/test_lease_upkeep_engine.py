"""Lease upkeep checks out from its own pool, and only upkeep workers may use it."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import SecretBytes
from sqlalchemy import Engine, event
from sqlalchemy.pool import QueuePool

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web import app as app_module
from elspeth.web import async_workers
from elspeth.web.app import create_app
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.config import WebSettings
from elspeth.web.coordination import lifecycle as lifecycle_module
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceIdentity,
    current_compatibility_key,
)
from elspeth.web.coordination.membership_lifecycle import RegisteredWebInstanceMembership
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.coordination.upkeep_engine import (
    LeaseUpkeepCheckoutRefused,
    create_lease_upkeep_engine,
    has_separate_lease_upkeep_pool,
    lease_upkeep_authority,
)
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import SessionForkAuthority
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )


@pytest.fixture()
def guarded_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_lease_upkeep_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    assert engine is not None
    try:
        yield engine
    finally:
        engine.dispose()


def _checked_out(engine: Engine) -> int:
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()


def _select_one(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("SELECT 1").scalar_one())


def _context() -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(session_id="session-1", operation_id="operation-1", lease_token="token-1", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )


# --- the checkout guard, both directions -------------------------------------------


def test_upkeep_worker_checkout_passes(guarded_engine: Engine) -> None:
    async def scenario() -> None:
        try:
            assert await async_workers.run_lease_upkeep_in_worker(_select_one, guarded_engine) == 1
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())
    assert _checked_out(guarded_engine) == 0


def test_main_thread_checkout_is_refused(guarded_engine: Engine) -> None:
    with pytest.raises(LeaseUpkeepCheckoutRefused):
        _select_one(guarded_engine)
    assert _checked_out(guarded_engine) == 0


def test_thread_named_like_an_upkeep_worker_is_refused(guarded_engine: Engine) -> None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _select_one(guarded_engine)
        except BaseException as error:
            errors.append(error)

    impostor = threading.Thread(target=run, name="lease-upkeep_0")
    impostor.start()
    impostor.join(5)
    assert not impostor.is_alive()
    assert len(errors) == 1
    assert type(errors[0]) is LeaseUpkeepCheckoutRefused
    assert _checked_out(guarded_engine) == 0


@pytest.mark.parametrize("runner", ["shared", "auth-audit"])
def test_other_worker_pools_are_refused(guarded_engine: Engine, runner: str) -> None:
    async def scenario() -> None:
        try:
            with pytest.raises(LeaseUpkeepCheckoutRefused):
                if runner == "shared":
                    await async_workers.run_sync_in_worker(_select_one, guarded_engine)
                else:
                    await async_workers.run_auth_audit_in_worker(_select_one, guarded_engine)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())
    assert _checked_out(guarded_engine) == 0


def test_refused_checkouts_do_not_consume_upkeep_connections(guarded_engine: Engine) -> None:
    for _ in range(async_workers.LEASE_UPKEEP_MAX_WORKERS + 3):
        with pytest.raises(LeaseUpkeepCheckoutRefused):
            _select_one(guarded_engine)

    async def scenario() -> None:
        try:
            assert await async_workers.run_lease_upkeep_in_worker(_select_one, guarded_engine) == 1
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())


def test_in_memory_sqlite_has_no_separate_pool(tmp_path: Path) -> None:
    assert not has_separate_lease_upkeep_pool("sqlite://")
    assert not has_separate_lease_upkeep_pool("sqlite:///:memory:")
    assert has_separate_lease_upkeep_pool(f"sqlite:///{tmp_path / 'sessions.db'}")
    assert has_separate_lease_upkeep_pool("postgresql+psycopg://user@db.example/sessions")


# --- the sibling carrier ------------------------------------------------------------


def test_sibling_must_be_the_same_class_and_must_not_nest(tmp_path: Path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    try:
        leaf = SQLiteLocalSessionOperationAuthority(engine)
        carrier = SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=leaf)
        assert carrier.lease_upkeep_authority is leaf
        assert lease_upkeep_authority(carrier) is leaf
        assert lease_upkeep_authority(leaf) is leaf
        with pytest.raises(ValueError, match="must not carry its own upkeep authority"):
            SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=carrier)

        class _Subclass(SQLiteLocalSessionOperationAuthority):
            pass

        with pytest.raises(TypeError, match="same authority class"):
            SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=_Subclass(engine))
        membership_leaf = RepositoryWebInstanceMembershipAuthority(engine)
        membership = RepositoryWebInstanceMembershipAuthority(engine, lease_upkeep_authority=membership_leaf)
        assert membership.lease_upkeep_authority is membership_leaf
        with pytest.raises(ValueError, match="must not carry its own upkeep authority"):
            RepositoryWebInstanceMembershipAuthority(engine, lease_upkeep_authority=membership)
    finally:
        engine.dispose()


# --- production wiring --------------------------------------------------------------


class _SiteRecorder:
    """Replaces the upkeep verbs on both authorities and records which one ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, main: object, sibling: object) -> None:
        self.calls: list[tuple[str, str]] = []
        for label, authority in (("main", main), ("sibling", sibling)):
            for verb in ("renew", "renew_fork_child_lease", "release"):
                monkeypatch.setattr(authority, verb, self._recorder(label, verb))

    def _recorder(self, label: str, verb: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> SessionOperationContext | None:
            self.calls.append((label, verb))
            return None if verb == "release" else _context()

        return record


async def _drive_every_upkeep_site(authority: object) -> None:
    """Run each of the six lease-upkeep call sites once against ``authority``."""
    typed = cast(SQLiteLocalSessionOperationAuthority, authority)
    context = _context()
    lease = SessionOperationLease(typed, context, lease_seconds=30, renew_interval_seconds=0.01)
    fork_lease = SessionOperationLease(
        typed, context, lease_seconds=30, renew_interval_seconds=0.01, fork_authority=cast(SessionForkAuthority, object())
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        await fork_lease.close()
        await lease.close()
    await lifecycle_module._release_acquired_context(typed, context)
    assert await lifecycle_module._capture_adopt_release_error(typed, context) is None


def _assert_upkeep_wiring(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upkeep engine is distinct and every upkeep call site resolves to the sibling."""
    upkeep_engine = app.state.lease_upkeep_engine
    assert upkeep_engine is not None
    assert upkeep_engine is not app.state.session_engine
    pool = upkeep_engine.pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == async_workers.LEASE_UPKEEP_MAX_WORKERS
    authority = app.state.session_service.session_operation_authority
    sibling = lease_upkeep_authority(authority)
    assert sibling is not authority
    assert sibling._engine is upkeep_engine
    recorder = _SiteRecorder(monkeypatch, authority, sibling)

    async def scenario() -> None:
        try:
            await _drive_every_upkeep_site(authority)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())
    sites = {verb for label, verb in recorder.calls}
    assert sites == {"renew", "renew_fork_child_lease", "release"}
    assert [call for call in recorder.calls if call[0] == "main"] == []


@pytest.fixture()
def built_app(tmp_path: Path) -> Iterator[Callable[[], FastAPI]]:
    apps: list[FastAPI] = []

    def build() -> FastAPI:
        app = create_app(_settings(tmp_path))
        apps.append(app)
        return app

    try:
        yield build
    finally:
        for app in apps:
            app.state._session_engine_finalizer()


def test_create_app_wires_every_upkeep_site_to_the_upkeep_engine(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_upkeep_wiring(built_app(), monkeypatch)


def test_wiring_check_fails_when_the_upkeep_engine_is_omitted(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_create_lease_upkeep_engine", lambda *_args, **_kwargs: None)
    with pytest.raises(AssertionError):
        _assert_upkeep_wiring(built_app(), monkeypatch)


def test_wiring_check_fails_when_the_sibling_is_omitted(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoSibling(SQLiteLocalSessionOperationAuthority):
        def __init__(self, engine: Engine, **kwargs: Any) -> None:
            kwargs.pop("lease_upkeep_authority", None)
            super().__init__(engine, **kwargs)

    monkeypatch.setattr(app_module, "SQLiteLocalSessionOperationAuthority", _NoSibling)
    with pytest.raises(AssertionError):
        _assert_upkeep_wiring(built_app(), monkeypatch)


def test_session_engine_finalizer_disposes_both_pools(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    app = built_app()
    disposed: list[str] = []
    main_engine = app.state.session_engine
    upkeep_engine = app.state.lease_upkeep_engine

    def failing_upkeep_dispose() -> None:
        disposed.append("upkeep")
        raise RuntimeError("upkeep dispose failed")

    real_main_dispose = main_engine.dispose
    real_upkeep_dispose = upkeep_engine.dispose

    def recording_main_dispose() -> None:
        disposed.append("main")
        real_main_dispose()

    monkeypatch.setattr(upkeep_engine, "dispose", failing_upkeep_dispose)
    monkeypatch.setattr(main_engine, "dispose", recording_main_dispose)
    with pytest.raises(RuntimeError, match="upkeep dispose failed"):
        app.state._session_engine_finalizer()
    assert disposed == ["upkeep", "main"]
    real_upkeep_dispose()


# --- the membership heartbeat ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_heartbeat_checks_out_from_the_upkeep_pool_and_register_does_not(
    tmp_path: Path, guarded_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(main_engine)
    upkeep_checkouts: list[str] = []
    event.listen(guarded_engine, "checkout", lambda *_args: upkeep_checkouts.append(threading.current_thread().name))
    authority = RepositoryWebInstanceMembershipAuthority(
        main_engine, lease_upkeep_authority=RepositoryWebInstanceMembershipAuthority(guarded_engine)
    )
    identity = WebInstanceIdentity(
        instance_id=f"sqlite-{uuid4()}",
        deployment_target="kubernetes",
        deployment_generation="generation-1",
        compatibility_key=current_compatibility_key(),
        image_digest="sha256:membership-test",
        revision_label="revision-1",
    )
    draining = threading.Event()
    owner = ApplicationFinalizerOwner()
    membership = RegisteredWebInstanceMembership(
        authority,
        identity,
        lease_seconds=3,
        interval_seconds=1,
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining),
        instance_draining=draining,
        finalizer_owner=owner,
    )
    owner.seal()
    monkeypatch.setattr(async_workers, "_APPLICATION_FINALIZER_OWNER", owner)
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", draining)
    try:
        # Register runs on the shared pool; on the guarded pool it would be refused.
        await membership.start()
        assert upkeep_checkouts == []
        await _wait_until_true(lambda: len(upkeep_checkouts) >= 1, seconds=5)
        assert all(name.startswith("lease-upkeep") for name in upkeep_checkouts)
    finally:
        try:
            await asyncio.wait_for(membership.begin_drain(), timeout=5)
        finally:
            try:
                await asyncio.wait_for(membership.stop(), timeout=5)
            finally:
                try:
                    await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)
                finally:
                    main_engine.dispose()


async def _wait_until_true(predicate: Callable[[], bool], *, seconds: float) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)
