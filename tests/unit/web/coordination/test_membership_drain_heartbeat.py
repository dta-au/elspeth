"""Process drain stops fresh heartbeat recurrence, preserving physical originals."""

import asyncio
import threading

import pytest
from tests.unit.web.coordination.test_membership_authority import _identity, _row
from tests.unit.web.coordination.test_membership_authority import engine as _engine
from tests.unit.web.coordination.test_membership_authority import membership_factory as _membership_factory

from elspeth.web import async_workers
from elspeth.web.coordination.membership_authority import RepositoryWebInstanceMembershipAuthority
from elspeth.web.coordination.membership_lifecycle import MembershipShutdownOutcome
from elspeth.web.required_executor import RequiredGenerationUnavailable

engine = _engine
membership_factory = _membership_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("during_cadence", (False, True))
async def test_own_process_drain_joins_heartbeat_without_starting_another_call(engine, membership_factory, monkeypatch, during_cadence):
    authority = RepositoryWebInstanceMembershipAuthority(engine)
    identity = _identity()
    membership = membership_factory(authority, identity, lease_seconds=30, interval_seconds=1)
    calls = []
    actual_heartbeat = RepositoryWebInstanceMembershipAuthority.heartbeat

    def observed_heartbeat(self, *args, **kwargs):
        calls.append(self)
        return actual_heartbeat(self, *args, **kwargs)

    monkeypatch.setattr(RepositoryWebInstanceMembershipAuthority, "heartbeat", observed_heartbeat)
    await membership.start()
    task = membership._heartbeat_task
    assert task is not None
    try:
        if during_cadence:
            # Let the newly created production loop enter its actual cadence.
            await asyncio.sleep(0)
            assert not task.done()
        membership.draining.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
        assert not task.cancelled() and task.exception() is None and calls == []
        assert membership._process_recovery.watchdog.signals == []
        assert await membership.begin_drain() is MembershipShutdownOutcome.RECORDED
    finally:
        assert await membership.stop() is MembershipShutdownOutcome.RECORDED
    row = _row(engine, identity.instance_id)
    assert row.state == "stopped" and row.lease_expires_at == row.last_heartbeat_at
    assert membership._heartbeat_task is None and async_workers.outstanding_admissions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("same_admission_class", (False, True))
async def test_already_entered_heartbeat_failure_survives_later_process_drain(
    engine, membership_factory, monkeypatch, same_admission_class
):
    authority = RepositoryWebInstanceMembershipAuthority(engine)
    identity = _identity()
    membership = membership_factory(authority, identity, lease_seconds=30, interval_seconds=1)
    entered, release = threading.Event(), threading.Event()
    original = (
        RequiredGenerationUnavailable("controlled physical heartbeat original")
        if same_admission_class
        else TypeError("controlled physical heartbeat original")
    )
    actual_heartbeat = RepositoryWebInstanceMembershipAuthority.heartbeat

    def physical_failure(self, *args, **kwargs):
        value = actual_heartbeat(self, *args, **kwargs)
        assert value.instance_id == identity.instance_id
        entered.set()
        assert release.wait(timeout=5), "physical heartbeat was not released by its test owner"
        raise original

    monkeypatch.setattr(RepositoryWebInstanceMembershipAuthority, "heartbeat", physical_failure)
    await membership.start()
    task = membership._heartbeat_task
    assert task is not None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        assert async_workers.outstanding_admissions() == 1 and not task.done()
        membership.draining.set()
        release.set()
        with pytest.raises(type(original)) as caught:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        assert caught.value is original and task.exception() is original
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(type(original)) as stopped:
            await membership.stop()
        assert stopped.value is original
    row = _row(engine, identity.instance_id)
    assert row.state == "stopped" and row.lease_expires_at == row.last_heartbeat_at
    assert async_workers.outstanding_admissions() == 0


@pytest.mark.asyncio
async def test_generation_refusal_without_own_drain_remains_a_heartbeat_failure(engine, membership_factory, monkeypatch):
    authority = RepositoryWebInstanceMembershipAuthority(engine)
    identity = _identity()
    membership = membership_factory(authority, identity, lease_seconds=30, interval_seconds=1)
    await membership.start()
    task = membership._heartbeat_task
    assert task is not None and not membership.draining.is_set()
    original = None
    try:
        with monkeypatch.context() as unavailable:
            marker = threading.Event()
            marker.set()
            unavailable.setattr(async_workers, "_GENERATION_UNAVAILABLE", marker)
            with pytest.raises(RequiredGenerationUnavailable) as refused:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            original = refused.value
            assert task.exception() is original
    finally:
        if original is None:
            await membership.stop()
        else:
            with pytest.raises(RequiredGenerationUnavailable) as stopped:
                await membership.stop()
            assert stopped.value is original
    assert membership.draining.is_set()
    row = _row(engine, identity.instance_id)
    assert row.state == "stopped" and row.lease_expires_at == row.last_heartbeat_at
    assert async_workers.outstanding_admissions() == 0
