"""Actual scoped metadata names keep their physical disposition SQL clock."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import event

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer.continuation_env_inventory import _read_current_env_names, read_disposition_env_names
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import RepositoryUserSecretAuthority, UserSecretStore
from elspeth.web.sessions import continuation_sql_deadline, locking
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.engine import create_session_engine
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.continuation_inventory import inventory_advisor
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners

operation_store = _operation_store


@asynccontextmanager
async def _inventory(store, monkeypatch, *, mode="local"):
    running, owner, signal = _owners(store)
    owner.begin_settlement()
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", signal.draining)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
        ),
        sql_deadline_owner=owner,
    )
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    with store[0].begin() as conn:
        ensure_test_identity(conn, identity_id="bob")
    writer = RepositoryUserSecretAuthority(store[0])
    for user, provider, name in (
        ("alice", "local", "LOCAL_ONLY"),
        ("alice", "local", "SHARED"),
        ("alice", "entra", "ENTRA_ONLY"),
        ("bob", "local", "BOB_ONLY"),
    ):
        # Inventory must not fetch or decrypt even deliberately invalid cipher data.
        writer.upsert_encrypted_secret(
            name=name, user_id=user, auth_provider_type=provider, encrypted_value=b"not-ciphertext", salt=b"0" * 16
        )
    user_store = UserSecretStore(engine=store[0], master_key="unused-metadata-test-master-key")
    secret_service = WebSecretService(
        user_store, ServerSecretStore(("SHARED", "SERVER_ONLY", "UNAVAILABLE")), user_secrets_enabled=mode != "server-only"
    )
    resolver = ScopedSecretResolver(secret_service, "entra" if mode == "entra" else "local")
    advisor = inventory_advisor(store[3], secret_service=None if mode == "none" else resolver)
    try:
        yield running, owner, signal, work, advisor, user_store
    finally:
        coordinator.assert_completed()
        assert async_workers.outstanding_admissions() == 0


async def _names(store, work, advisor):
    return await read_disposition_env_names(advisor=advisor, authority=store[2], required_work=work)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("local", "entra", "server-only", "none"))
async def test_actual_scoped_names_and_metadata_projection_survive_process_drain(operation_store, monkeypatch, mode):
    async with _inventory(operation_store, monkeypatch, mode=mode) as (running, owner, signal, work, advisor, _user_store):
        expected = {
            "local": frozenset(("LOCAL_ONLY", "SHARED", "SERVER_ONLY", "UNAVAILABLE")),
            "entra": frozenset(("ENTRA_ONLY", "SHARED", "SERVER_ONLY", "UNAVAILABLE")),
            "server-only": frozenset(("SHARED", "SERVER_ONLY", "UNAVAILABLE")),
            "none": frozenset(),
        }[mode]
        assert await _read_current_env_names(advisor, str(running.claim.session_id)) == expected
        metadata = []

        def observe(conn, cursor, statement, parameters, context, executemany):
            if "FROM user_secrets" in statement:
                assert context.compiled is not None
                metadata.append(
                    (
                        tuple(column.name for column in context.compiled.statement.selected_columns),
                        locking._CONTINUATION_SQL_DEADLINE.get(),
                        locking._CONTINUATION_METADATA_ENGINE.get(),
                        threading.get_ident(),
                    )
                )

        def forbidden(*args, **kwargs):
            pytest.fail("names-only inventory attempted resolution/decryption")

        monkeypatch.setattr(UserSecretStore, "get_secret", forbidden)
        monkeypatch.setattr(UserSecretStore, "has_secret", forbidden)
        monkeypatch.setattr(ScopedSecretResolver, "resolve", forbidden)
        monkeypatch.setattr(ScopedSecretResolver, "has_ref", forbidden)
        event.listen(operation_store[0], "before_cursor_execute", observe)
        try:
            signal.request()
            assert await _names(operation_store, work, advisor) == expected
            if mode in ("local", "entra"):
                assert len(metadata) == 1
                columns, scope, engine, physical_thread = metadata[0]
                assert columns == ("name",) and scope == (owner, False)
                assert engine is operation_store[0] and physical_thread != threading.get_ident()
            else:
                assert metadata == []
            with pytest.raises(RequiredGenerationUnavailable):
                await operation_store[3].get_session(running.claim.session_id)
            assert locking._CONTINUATION_SQL_DEADLINE.get() is None
            assert locking._CONTINUATION_METADATA_ENGINE.get() is None
        finally:
            event.remove(operation_store[0], "before_cursor_execute", observe)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("physical-owner", "missing-clock", "authority-engine", "metadata-engine", "expired", "released"))
async def test_disposition_names_refuse_foreign_released_or_expired_owner(operation_store, monkeypatch, tmp_path, fault):
    async with _inventory(operation_store, monkeypatch) as (running, owner, signal, work, advisor, user_store):
        foreign_engine = create_session_engine(f"sqlite:///{tmp_path / 'foreign-metadata.db'}")
        try:
            signal.request()
            with monkeypatch.context() as changed:
                if fault == "physical-owner":
                    changed.setattr(owner, "running", replace(running))
                elif fault == "missing-clock":
                    changed.setattr(work.coordinator, "_sql_deadline_owner", None)
                elif fault == "authority-engine":
                    changed.setattr(operation_store[2], "_engine", foreign_engine)
                elif fault == "metadata-engine":
                    changed.setattr(user_store, "_engine", foreign_engine)
                elif fault == "released":
                    await asyncio.to_thread(operation_store[1].release, running.session_operation_context)
                else:
                    assert signal.started_at is not None

                    class ExpiredClock:
                        @staticmethod
                        def monotonic():
                            return signal.started_at + owner.drain_seconds + 1

                    changed.setattr(continuation_sql_deadline, "time", ExpiredClock)
                error = TimeoutError if fault == "expired" else ComposerOperationFenceLost if fault == "released" else AuditIntegrityError
                with pytest.raises(error):
                    await _names(operation_store, work, advisor)
            assert locking._CONTINUATION_SQL_DEADLINE.get() is None
            assert locking._CONTINUATION_METADATA_ENGINE.get() is None
        finally:
            foreign_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("sessions", "user_secrets"))
async def test_physical_metadata_error_is_preserved_and_scopes_are_removed(operation_store, monkeypatch, boundary):
    async with _inventory(operation_store, monkeypatch) as (_running, _owner, signal, work, advisor, _user_store):
        original = RuntimeError("controlled physical inventory SQL original")

        def fail(conn, cursor, statement, parameters, context, executemany):
            if f"FROM {boundary}" in statement:
                raise original

        event.listen(operation_store[0], "before_cursor_execute", fail)
        signal.request()
        try:
            with pytest.raises(RuntimeError) as caught:
                await _names(operation_store, work, advisor)
            assert caught.value is original
        finally:
            event.remove(operation_store[0], "before_cursor_execute", fail)
        assert await _names(operation_store, work, advisor) == frozenset(("LOCAL_ONLY", "SHARED", "SERVER_ONLY", "UNAVAILABLE"))
        assert locking._CONTINUATION_METADATA_ENGINE.get() is None


@pytest.mark.asyncio
async def test_delivered_cancellation_joins_actual_metadata_read_before_escaping(operation_store, monkeypatch):
    async with _inventory(operation_store, monkeypatch) as (_running, _owner, signal, work, advisor, _user_store):
        entered, release = threading.Event(), threading.Event()

        def hold(conn, cursor, statement, parameters, context, executemany):
            if "FROM user_secrets" in statement:
                entered.set()
                assert release.wait(timeout=5), "metadata read was not released by its test owner"

        event.listen(operation_store[0], "before_cursor_execute", hold)
        signal.request()
        task = asyncio.create_task(_names(operation_store, work, advisor))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
            task.cancel("original env metadata cancellation")
            await asyncio.sleep(0)
            assert not task.done() and async_workers.outstanding_admissions() == 1
            release.set()
            done, pending = await asyncio.wait((task,), timeout=3)
            assert done == {task} and pending == set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("original env metadata cancellation",)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(operation_store[0], "before_cursor_execute", hold)
        assert locking._CONTINUATION_SQL_DEADLINE.get() is None and locking._CONTINUATION_METADATA_ENGINE.get() is None
