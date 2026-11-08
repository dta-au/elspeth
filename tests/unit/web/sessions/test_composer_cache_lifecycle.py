"""Session advice follows real read and archive ownership boundaries."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.graph_recovery import CompositionGraphRead
from elspeth.web.sessions.models import composer_completion_events_table
from elspeth.web.sessions.protocol import CompositionStateData, SessionNotFoundError
from tests.fixtures.composer_session_cache import make_small_composer_state
from tests.unit.web.sessions.test_fork import _fork_session
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines
from tests.unit.web.sessions.test_routes import _install_restricted_plugin_policy, _make_app, _save_test_composition_state


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_current_state_reconstructs_each_read_but_reuses_only_equal_graph(tmp_path, monkeypatch, enabled):
    app, service = _make_app(tmp_path)
    registry = ComposerSessionCacheRegistry(enabled=enabled)
    app.state.composer_session_cache_registry = registry
    session = await service.create_session("alice", "Read cache", "local")
    data = CompositionStateData(metadata_={"name": "original", "description": ""})
    await _save_test_composition_state(service, session.id, data, provenance="session_seed")
    original = CompositionState.validate
    seen: list[CompositionState] = []

    def validate(state):
        seen.append(state)
        return original(state)

    monkeypatch.setattr(CompositionState, "validate", validate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(f"/api/sessions/{session.id}/state")
        second = await client.get(f"/api/sessions/{session.id}/state")
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert len(seen) == (1 if enabled else 2)
        await _save_test_composition_state(service, session.id, data, provenance="session_seed")
        before = len(seen)
        equal = await client.get(f"/api/sessions/{session.id}/state")
        assert equal.json()["version"] > first.json()["version"]
        assert len(seen) - before == (0 if enabled else 1)
        await _save_test_composition_state(
            service, session.id, CompositionStateData(metadata_={"name": "changed", "description": ""}), provenance="session_seed"
        )
        before = len(seen)
        changed = await client.get(f"/api/sessions/{session.id}/state")
        assert changed.status_code == 200
        assert changed.json()["metadata"]["name"] == "changed"
        assert len(seen) - before == 1
    assert registry.snapshot().borrows == registry.snapshot().pins == 0


@pytest.mark.asyncio
async def test_state_read_does_not_borrow_before_ownership_hydration_or_absent_state_checks(tmp_path):
    app, service = _make_app(tmp_path)
    registry = app.state.composer_session_cache_registry
    session = await service.create_session("alice", "Empty", "local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch.object(registry, "borrow", wraps=registry.borrow) as borrow:
            missing = await client.get(f"/api/sessions/{uuid.uuid4()}/state")
            empty = await client.get(f"/api/sessions/{session.id}/state")
            assert missing.status_code == 404 and empty.json() is None
            read = await service.read_current_composition_graph(session.id)
            damaged = CompositionGraphRead(head=read.head, record=None, hydration_error="ValueError")
            with patch.object(service, "read_current_composition_graph", return_value=damaged):
                refused = await client.get(f"/api/sessions/{session.id}/state")
            assert refused.status_code == 409
            borrow.assert_not_called()


@pytest.mark.asyncio
async def test_warm_state_read_still_reports_current_plugin_policy(tmp_path):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Policy changes", "local")
    raw = make_small_composer_state().to_dict()
    await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources=raw["sources"],
            nodes=raw["nodes"],
            edges=raw["edges"],
            outputs=raw["outputs"],
            metadata_=raw["metadata"],
        ),
        provenance="session_seed",
    )
    original = CompositionState.validate
    walks = []

    def validate(state):
        walks.append(state)
        return original(state)

    with patch.object(CompositionState, "validate", validate):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get(f"/api/sessions/{session.id}/state")
            _install_restricted_plugin_policy(app, PluginId("source", "csv"))
            second = await client.get(f"/api/sessions/{session.id}/state")
    assert first.status_code == second.status_code == 200
    assert first.json()["plugin_policy_findings"] == []
    assert second.json()["plugin_policy_findings"]
    assert len(walks) == 1


@pytest.mark.asyncio
async def test_real_session_fork_uses_distinct_owner_and_disclosure(tmp_path):
    app, service = _make_app(tmp_path)
    source = await service.create_session("alice", "Parent", "local")
    message = await service.add_message(source.id, "user", "Parent request", writer_principal="route_user_message")
    child, _, _ = await _fork_session(
        service,
        source_session_id=source.id,
        fork_message_id=message.id,
        new_message_content="Fork request",
        user_id="alice",
        auth_provider_type="local",
    )
    registry = app.state.composer_session_cache_registry
    with (
        registry.borrow(SessionCacheKey("local:alice", str(source.id))) as parent,
        registry.borrow(SessionCacheKey("local:alice", str(child.id))) as fork,
    ):
        assert parent.cache is not None and fork.cache is not None and parent.cache is not fork.cache
        parent.cache.mark_schema_loaded("snapshot", "source", "csv")
        state = make_small_composer_state()
        ComposerAnalysis(parent.cache).validate_raw(state)
        before = registry.snapshot()
        ComposerAnalysis(fork.cache).validate_raw(state)
        assert registry.snapshot().misses == before.misses + 1
        assert fork.cache.schemas_loaded("snapshot") == frozenset()
        assert parent.cache.schemas_loaded("snapshot") == frozenset({("source", "csv")})


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_archive_acquisition_reserves_even_cold_reads_until_real_worker_settles(tmp_path, monkeypatch, warm, cancel):
    app, service = _make_app(tmp_path)
    registry = app.state.composer_session_cache_registry
    service._retire_composer_session = registry.retire_session
    session = await service.create_session("alice", "Archive barrier", "local")
    await _save_test_composition_state(
        service, session.id, CompositionStateData(metadata_={"name": "readable", "description": ""}), provenance="session_seed"
    )
    key = SessionCacheKey("local:alice", str(session.id))
    if warm:
        with registry.borrow(key) as prior:
            assert prior.cache is not None
    entered, release = threading.Event(), threading.Event()
    original = service.session_operation_authority.acquire

    def acquire(**kwargs):
        context = original(**kwargs)
        if kwargs["operation_kind"] is SessionOperationKind.ARCHIVE:
            entered.set()
            assert release.wait(10), "test must release archive worker"
        return context

    monkeypatch.setattr(service.session_operation_authority, "acquire", acquire)
    archive = asyncio.create_task(service.archive_session(session.id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert registry.snapshot().retirements == 1
        assert registry.snapshot().retained_sessions == 0
        # A second genuine attempt refuses while the first holds authority;
        # releasing its reservation cannot release the first attempt's token.
        with pytest.raises(SessionOperationConflictError):
            await service.archive_session(session.id)
        assert registry.snapshot().retirements == 1
        if cancel:
            archive.cancel()
            await asyncio.sleep(0)
            archive.cancel()
            await asyncio.sleep(0)
            assert not archive.done()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/sessions/{session.id}/state")
        assert response.status_code == 200
        assert registry.snapshot().retained_sessions == 0
        with registry.borrow(SessionCacheKey("another-principal", str(session.id))) as bypass:
            assert bypass.cache is None
    finally:
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(archive, 10)
        else:
            await asyncio.wait_for(archive, 10)
    assert registry.snapshot().retirements == 0
    assert registry.snapshot().weight_bytes == 0
    with registry.borrow(key) as after:
        assert after.cache is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["missing", "soft", "no-directory", "precommit", "postcommit"])
async def test_real_archive_outcomes_release_the_retirement_attempt(tmp_path, monkeypatch, outcome):
    app, service = _make_app(tmp_path)
    registry = app.state.composer_session_cache_registry
    service._retire_composer_session = registry.retire_session
    session = await service.create_session("alice", "Archive outcome", "local")
    sid = uuid.uuid4() if outcome == "missing" else session.id
    if outcome in {"precommit", "postcommit"}:
        service._data_dir = tmp_path
    if outcome == "soft":
        state = await _save_test_composition_state(service, sid, CompositionStateData(), provenance="session_seed")
        with service._engine.begin() as conn:
            conn.execute(
                insert(composer_completion_events_table).values(
                    id=str(uuid.uuid4()),
                    session_id=str(sid),
                    composition_state_id=str(state.id),
                    event_type="export_yaml",
                    actor="user:alice",
                    created_at=datetime.now(UTC),
                )
            )
    with registry.borrow(SessionCacheKey("local:alice", str(sid))) as old:
        assert old.cache is not None
    worker = service_module.run_sync_in_worker

    async def observed_worker(function, *args, **kwargs):
        assert registry.snapshot().retirements == 1
        assert registry.snapshot().retained_sessions == 0
        if outcome == "precommit" and function is service_module.list_archive_quarantine_manifests:
            raise RuntimeError("precommit control")
        if outcome == "postcommit" and function is service_module.purge_archive_quarantine:
            raise service_module.QuarantineCleanupError("postcommit control")
        return await worker(function, *args, **kwargs)

    monkeypatch.setattr(service_module, "run_sync_in_worker", observed_worker)
    if outcome in {"missing", "precommit", "postcommit"}:
        expected = (
            SessionNotFoundError
            if outcome == "missing"
            else RuntimeError
            if outcome == "precommit"
            else service_module.QuarantineCleanupError
        )
        with pytest.raises(expected):
            await service.archive_session(sid)
    else:
        await service.archive_session(sid)
    assert registry.snapshot().retirements == registry.snapshot().retained_sessions == registry.snapshot().weight_bytes == 0
    monkeypatch.setattr(service_module, "run_sync_in_worker", worker)
    if outcome == "soft":
        assert (await service.get_session(sid)).archived_at is not None


@pytest.mark.asyncio
async def test_postcommit_cancellation_keeps_retirement_until_cleanup_settles(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path)
    registry = app.state.composer_session_cache_registry
    service._retire_composer_session = registry.retire_session
    service._data_dir = tmp_path
    session = await service.create_session("alice", "Cleanup barrier", "local")
    entered, release = asyncio.Event(), asyncio.Event()
    worker = service_module.run_sync_in_worker

    async def controlled_worker(function, *args, **kwargs):
        if function is service_module.purge_archive_quarantine:
            entered.set()
            await release.wait()
        return await worker(function, *args, **kwargs)

    monkeypatch.setattr(service_module, "run_sync_in_worker", controlled_worker)
    task = asyncio.create_task(service.archive_session(session.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with registry.borrow(SessionCacheKey("local:alice", str(session.id))) as bypass:
            assert bypass.cache is None
        assert registry.snapshot().retirements == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert registry.snapshot().retirements == registry.snapshot().retained_sessions == 0
