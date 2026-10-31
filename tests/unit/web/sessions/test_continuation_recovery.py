"""Production reaper regressions through the pre-existing worker entry point."""

import asyncio

import pytest
from sqlalchemy import select, update

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_composer_async_worker import _file_app, _worker
from tests.unit.web.sessions.test_continuation_recovery_candidates import _started

operation_store = _operation_store


def worker_for(store, tmp_path, authority):
    app, _service, auxiliary_engine, composer = _file_app(tmp_path)
    app.state.session_service = store[3]
    app.state.session_engine = store[0]
    app.state.composer_async_operation_authority = authority
    worker = _worker(app, authority)
    return worker, auxiliary_engine, composer


def reaper(store, tmp_path):
    authority, record, claim, context = _started(store)
    worker, auxiliary_engine, composer = worker_for(store, tmp_path, authority)
    return worker, authority, record, claim, context, auxiliary_engine, composer


@pytest.mark.asyncio
async def test_reap_once_recovers_released_v2_owner_before_claim(operation_store, tmp_path):
    worker, authority, record, claim, context, auxiliary, composer = reaper(operation_store, tmp_path)
    engine, repository, _, _, _sid = operation_store
    try:
        assert await worker.reap_once() == 0, "live original owner is the negative control"
        repository.release(context)
        assert await worker.reap_once() == 1
        with engine.connect() as conn:
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).one()
            segment = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).one()
            history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).one()
            tags = conn.execute(select(events.c.tag).where(events.c.operation_id == record.operation_id)).scalars().all()
        assert parent.ownership_generation == segment.generation == claim.ownership_generation + 1
        assert parent.phase == "reconciling" and segment.state == "interrupted"
        assert history.revoked_at is not None and history.outcome == "interrupted"
        assert tags.count("recovered") == 1 and tags.count("settled") == 0
        assert await worker.reap_once() == 0
        assert composer.calls == 0
        (next_claim,) = authority.claim_segment(limit=1)
        current = repository.start_composer_segment(
            next_claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local"
        )
        assert current.fence.operation_epoch > context.fence.operation_epoch
        repository.release(current)
    finally:
        auxiliary.dispose()


@pytest.mark.asyncio
async def test_reaper_excludes_locally_owned_and_adoption_pending_jobs(operation_store, tmp_path, monkeypatch):
    worker, _, record, _, context, auxiliary, composer = reaper(operation_store, tmp_path)
    operation_store[1].release(context)
    key = record.session_id, record.operation_id
    task = asyncio.current_task()
    assert task is not None
    worker._jobs[key] = task
    restarts = []
    monkeypatch.setattr(worker._process_recovery, "request_shutdown", lambda: restarts.append("required"))
    try:
        assert await worker.reap_once() == 0
        del worker._jobs[key]
        worker._adoption_recovery_pending.add(key)
        assert await worker.reap_once() == 0 and restarts == ["required"]
        worker._adoption_recovery_pending.remove(key)
        assert await worker.reap_once() == 1 and composer.calls == 0
    finally:
        worker._jobs.pop(key, None)
        worker._adoption_recovery_pending.discard(key)
        auxiliary.dispose()


@pytest.mark.parametrize("postgres,pinned", ((True, True), (True, False), (False, True)))
def test_pinned_postgres_identity_refuses_before_registration_and_database_access(tmp_path, monkeypatch, postgres, pinned):
    from elspeth.web import app as app_module
    from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
    from tests.unit.web.test_app import _settings

    class IdentityAccepted(Exception):
        pass

    settings = _settings(
        tmp_path,
        instance_id="pinned-owner" if pinned else None,
        session_db_url="postgresql://localhost/unopened" if postgres else f"sqlite:///{tmp_path / 'unopened.db'}",
        landscape_url="postgresql://localhost/unopened" if postgres else f"sqlite:///{tmp_path / 'landscape.db'}",
    )
    reached = []

    def identity_after_policy(_settings):
        reached.append("identity")
        raise IdentityAccepted

    monkeypatch.setattr(app_module, "resolve_instance_id", identity_after_policy)
    expected = ValueError if postgres and pinned else IdentityAccepted
    with pytest.raises(expected) as failure:
        app_module.create_app(settings, process_watchdog_factory=OwnedTestProcessWatchdog)
    if postgres and pinned:
        assert "fresh instance identity" in str(failure.value) and reached == []
    else:
        assert reached == ["identity"]


@pytest.mark.asyncio
async def test_reaper_stop_marker_wins_discovery_to_writer_race(operation_store, tmp_path, monkeypatch):
    worker, authority, record, claim, context, auxiliary, composer = reaper(operation_store, tmp_path)
    engine, repository, _, _, _sid = operation_store
    repository.release(context)
    original = authority.list_recoverable_segments

    def stop_after_discovery(*, limit):
        candidates = original(limit=limit)
        assert len(candidates) == 1 and candidates[0].cancel_requested_at is None
        with engine.begin() as conn:
            assert (
                conn.execute(
                    update(jobs).where(jobs.c.operation_id == record.operation_id).values(cancel_requested_at=database_now(conn))
                ).rowcount
                == 1
            )
        return candidates

    monkeypatch.setattr(authority, "list_recoverable_segments", stop_after_discovery)
    try:
        assert await worker.reap_once() == 1
        with engine.connect() as conn:
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).one()
            tags = conn.execute(select(events.c.tag).where(events.c.operation_id == record.operation_id)).scalars().all()
        assert parent.status == "failed" and parent.failure_code == "request_cancelled"
        assert parent.ownership_generation == claim.ownership_generation
        assert tags.count("settled") == 1 and "recovered" not in tags
        assert authority.claim_segment(limit=1) == () and composer.calls == 0
    finally:
        auxiliary.dispose()


@pytest.mark.asyncio
async def test_periodic_sweep_recovers_owner_lost_after_startup(operation_store, tmp_path, monkeypatch):
    worker, _, _, _, context, auxiliary, composer = reaper(operation_store, tmp_path)
    assert await worker.reap_once() == 0
    operation_store[1].release(context)
    claims_seen = asyncio.Event()

    async def claim_after_recovery():
        with operation_store[0].connect() as conn:
            assert conn.execute(select(segments.c.state)).scalar_one() == "interrupted"
        worker._stopping = True
        claims_seen.set()
        return 0

    monkeypatch.setattr(worker, "_claim_once", claim_after_recovery)
    try:
        await worker._loop()
        assert claims_seen.is_set() and composer.calls == 0
    finally:
        auxiliary.dispose()
