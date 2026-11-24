"""Lease upkeep checks out from its own pool, and only upkeep workers may use it."""

from __future__ import annotations

import asyncio
import pickle
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
from elspeth.web.coordination import membership_lifecycle as membership_lifecycle_module
from elspeth.web.coordination import upkeep_engine as upkeep_module
from elspeth.web.coordination.contracts import CloseReleaseOutcome
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
    LeaseUpkeepSiblingRefusal,
    LeaseUpkeepSiblingRefused,
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


def test_factory_builds_the_upkeep_pool_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []
    url = f"sqlite:///{tmp_path / 'sessions.db'}"

    def recording_factory(factory_url: str, **kwargs: Any) -> Engine:
        seen.append(kwargs)
        assert factory_url == url
        # SQLite takes no connect_timeout; the recorded kwargs are the contract.
        return create_session_engine(factory_url, **{key: value for key, value in kwargs.items() if key != "connect_args"})

    monkeypatch.setattr(upkeep_module, "create_session_engine", recording_factory)
    engines = [create_lease_upkeep_engine(url), create_lease_upkeep_engine(url, connect_timeout_seconds=7)]
    try:
        shape = {"pool_size": async_workers.LEASE_UPKEEP_MAX_WORKERS, "max_overflow": 0, "pool_pre_ping": True}
        assert seen == [shape, {**shape, "connect_args": {"connect_timeout": 7}}]
    finally:
        for engine in engines:
            assert engine is not None
            engine.dispose()


def test_factory_returns_the_engine_with_the_guard_already_attached(tmp_path: Path) -> None:
    # The only unguarded checkout is the factory's own construction-time probe.
    engine = create_lease_upkeep_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    assert engine is not None
    try:
        assert event.contains(engine, "checkout", upkeep_module._refuse_checkout_off_upkeep_worker)
        with pytest.raises(LeaseUpkeepCheckoutRefused):
            _select_one(engine)
    finally:
        engine.dispose()


# --- the sibling carrier ------------------------------------------------------------


def test_sibling_must_be_the_same_class_and_must_not_nest(tmp_path: Path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    try:
        leaf = SQLiteLocalSessionOperationAuthority(engine)
        carrier = SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=leaf)
        assert carrier.lease_upkeep_authority is leaf
        assert lease_upkeep_authority(carrier) is leaf
        assert lease_upkeep_authority(leaf) is leaf
        with pytest.raises(LeaseUpkeepSiblingRefused) as nested:
            SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=carrier)
        assert nested.value.reason is LeaseUpkeepSiblingRefusal.NESTED

        class _Subclass(SQLiteLocalSessionOperationAuthority):
            pass

        with pytest.raises(LeaseUpkeepSiblingRefused) as wrong_class:
            SQLiteLocalSessionOperationAuthority(engine, lease_upkeep_authority=_Subclass(engine))
        assert wrong_class.value.reason is LeaseUpkeepSiblingRefusal.WRONG_CLASS
        membership_leaf = RepositoryWebInstanceMembershipAuthority(engine)
        membership = RepositoryWebInstanceMembershipAuthority(engine, lease_upkeep_authority=membership_leaf)
        assert membership.lease_upkeep_authority is membership_leaf
        with pytest.raises(LeaseUpkeepSiblingRefused) as membership_nested:
            RepositoryWebInstanceMembershipAuthority(engine, lease_upkeep_authority=membership)
        assert membership_nested.value.reason is LeaseUpkeepSiblingRefusal.NESTED
        with pytest.raises(LeaseUpkeepSiblingRefused) as membership_wrong_class:
            RepositoryWebInstanceMembershipAuthority(engine, lease_upkeep_authority=cast(RepositoryWebInstanceMembershipAuthority, leaf))
        assert membership_wrong_class.value.reason is LeaseUpkeepSiblingRefusal.WRONG_CLASS
    finally:
        engine.dispose()


@pytest.mark.parametrize("reason", list(LeaseUpkeepSiblingRefusal))
def test_sibling_refusal_round_trips_through_pickle(reason: LeaseUpkeepSiblingRefusal) -> None:
    restored = pickle.loads(pickle.dumps(LeaseUpkeepSiblingRefused(reason)))
    assert type(restored) is LeaseUpkeepSiblingRefused
    assert restored.reason is reason


# --- production wiring --------------------------------------------------------------


class _SiteRecorder:
    """Replaces the upkeep verbs on both authorities and records which one ran, and on which pool."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, main: object, sibling: object) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        for label, authority in (("main", main), ("sibling", sibling)):
            for verb in ("renew", "renew_fork_child_lease", "release", "release_at_close"):
                monkeypatch.setattr(authority, verb, self._recorder(label, verb))

    def _recorder(self, label: str, verb: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> SessionOperationContext | CloseReleaseOutcome | None:
            self.calls.append((label, verb, async_workers.on_lease_upkeep_worker()))
            if verb == "release_at_close":
                return CloseReleaseOutcome.RELEASED_LIVE
            return None if verb == "release" else _context()

        return record

    def take(self) -> list[tuple[str, str, bool]]:
        calls = list(self.calls)
        self.calls.clear()
        return calls


async def _drive_each_upkeep_site(authority: object, recorder: _SiteRecorder) -> dict[str, list[tuple[str, str, bool]]]:
    """Run each of the six lease-upkeep call sites in turn and return what each one recorded."""
    typed = cast(SQLiteLocalSessionOperationAuthority, authority)
    context = _context()
    observed: dict[str, list[tuple[str, str, bool]]] = {}
    recorder.take()
    lease = SessionOperationLease(typed, context, lease_seconds=30, renew_interval_seconds=0.01)
    try:
        await asyncio.sleep(0.2)
    finally:
        await lease.close()
    observed["lease renew + release"] = recorder.take()
    fork_lease = SessionOperationLease(
        typed, context, lease_seconds=30, renew_interval_seconds=0.01, fork_authority=cast(SessionForkAuthority, object())
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        await fork_lease.close()
    observed["fork renew + release"] = recorder.take()
    await lifecycle_module._release_acquired_context(typed, context)
    observed["_release_acquired_context"] = recorder.take()
    assert await lifecycle_module._capture_adopt_release_error(typed, context) is None
    observed["_capture_adopt_release_error"] = recorder.take()
    return observed


def _assert_upkeep_wiring(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upkeep engine is distinct and guarded, and every upkeep call site resolves to the sibling on an upkeep worker."""
    upkeep_engine = app.state.lease_upkeep_engine
    assert upkeep_engine is not None
    assert upkeep_engine is not app.state.session_engine
    pool = upkeep_engine.pool
    assert isinstance(pool, QueuePool)
    assert (pool.size(), pool._max_overflow, pool._pre_ping) == (async_workers.LEASE_UPKEEP_MAX_WORKERS, 0, True)
    # The app's pool was disposed and re-created after schema init; the guard must survive that.
    with pytest.raises(LeaseUpkeepCheckoutRefused):
        _select_one(upkeep_engine)
    assert _checked_out(upkeep_engine) == 0
    authority = app.state.session_service.session_operation_authority
    sibling = lease_upkeep_authority(authority)
    assert sibling is not authority
    assert sibling._engine is upkeep_engine
    recorder = _SiteRecorder(monkeypatch, authority, sibling)

    async def scenario() -> dict[str, list[tuple[str, str, bool]]]:
        try:
            return await _drive_each_upkeep_site(authority, recorder)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    observed = asyncio.run(scenario())
    on_sibling_worker = {site: set(calls) for site, calls in observed.items()}
    assert on_sibling_worker == {
        "lease renew + release": {("sibling", "renew", True), ("sibling", "release_at_close", True)},
        "fork renew + release": {("sibling", "renew_fork_child_lease", True), ("sibling", "release_at_close", True)},
        "_release_acquired_context": {("sibling", "release", True)},
        "_capture_adopt_release_error": {("sibling", "release", True)},
    }
    releases = {site: sum(1 for call in calls if call[1] in ("release", "release_at_close")) for site, calls in observed.items()}
    assert releases == dict.fromkeys(observed, 1)


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
    app = built_app()
    with pytest.raises(AssertionError):
        _assert_upkeep_wiring(app, monkeypatch)


def test_wiring_check_fails_when_the_sibling_is_omitted(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoSibling(SQLiteLocalSessionOperationAuthority):
        def __init__(self, engine: Engine, **kwargs: Any) -> None:
            kwargs.pop("lease_upkeep_authority", None)
            super().__init__(engine, **kwargs)

    monkeypatch.setattr(app_module, "SQLiteLocalSessionOperationAuthority", _NoSibling)
    app = built_app()
    with pytest.raises(AssertionError):
        _assert_upkeep_wiring(app, monkeypatch)


class _UpkeepDisposeFailed(RuntimeError):
    """Sentinel raised by the patched upkeep dispose."""


def test_session_engine_finalizer_disposes_both_pools(built_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    app = built_app()
    disposed: list[str] = []
    main_engine = app.state.session_engine
    upkeep_engine = app.state.lease_upkeep_engine

    def failing_upkeep_dispose() -> None:
        disposed.append("upkeep")
        raise _UpkeepDisposeFailed

    real_main_dispose = main_engine.dispose
    real_upkeep_dispose = upkeep_engine.dispose

    def recording_main_dispose() -> None:
        disposed.append("main")
        real_main_dispose()

    monkeypatch.setattr(upkeep_engine, "dispose", failing_upkeep_dispose)
    monkeypatch.setattr(main_engine, "dispose", recording_main_dispose)
    with pytest.raises(_UpkeepDisposeFailed):
        app.state._session_engine_finalizer()
    assert disposed == ["upkeep", "main"]
    real_upkeep_dispose()


# --- the membership heartbeat ----------------------------------------------------


def _identity() -> WebInstanceIdentity:
    return WebInstanceIdentity(
        instance_id=f"sqlite-{uuid4()}",
        deployment_target="kubernetes",
        deployment_generation="generation-1",
        compatibility_key=current_compatibility_key(),
        image_digest="sha256:membership-test",
        revision_label="revision-1",
    )


def _membership(authority: RepositoryWebInstanceMembershipAuthority) -> RegisteredWebInstanceMembership:
    draining = threading.Event()
    return RegisteredWebInstanceMembership(
        authority,
        _identity(),
        lease_seconds=3,
        interval_seconds=1,
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining),
        instance_draining=draining,
        finalizer_owner=ApplicationFinalizerOwner(),
    )


def test_membership_heartbeat_resolves_through_the_single_resolver(
    tmp_path: Path, guarded_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    try:
        sibling = RepositoryWebInstanceMembershipAuthority(guarded_engine)
        carrier = RepositoryWebInstanceMembershipAuthority(main_engine, lease_upkeep_authority=sibling)
        bare = RepositoryWebInstanceMembershipAuthority(main_engine)
        resolved: list[RepositoryWebInstanceMembershipAuthority] = []
        real_resolver = upkeep_module.lease_upkeep_authority

        def recording_resolver(authority: RepositoryWebInstanceMembershipAuthority) -> RepositoryWebInstanceMembershipAuthority:
            resolved.append(authority)
            return real_resolver(authority)

        monkeypatch.setattr(membership_lifecycle_module, "lease_upkeep_authority", recording_resolver)
        with_sibling = _membership(carrier)
        without_sibling = _membership(bare)
        assert resolved == [carrier, bare]
        assert with_sibling._heartbeat_authority is sibling
        assert without_sibling._heartbeat_authority is bare
    finally:
        main_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_heartbeat_checks_out_from_the_upkeep_pool_and_register_does_not(
    tmp_path: Path, guarded_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(main_engine)
    upkeep_checkouts: list[bool] = []
    event.listen(guarded_engine, "checkout", lambda *_args: upkeep_checkouts.append(async_workers.on_lease_upkeep_worker()))
    authority = RepositoryWebInstanceMembershipAuthority(
        main_engine, lease_upkeep_authority=RepositoryWebInstanceMembershipAuthority(guarded_engine)
    )
    identity = _identity()
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
        assert all(upkeep_checkouts)
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
