"""Actual notification/child result custody under all cancellation orders."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.sessions.pipeline_rejection_custody import await_retained_originals, close_required_proposal_lease
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


def leaves(root):
    if isinstance(root, BaseExceptionGroup):
        return tuple(leaf for child in root.exceptions for leaf in leaves(child))
    return (root,)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("wrap_original", [False, True])
async def test_owned_join_rethrows_single_leaf_without_changing_original_cause_or_context(wrap_original):
    original = RuntimeError("actual sole owned child original")
    cause = ValueError("existing original cause")
    context = OSError("existing original context")
    original.__cause__ = cause
    original.__context__ = context
    root = BaseExceptionGroup("single explicit original", [original]) if wrap_original else original

    async def fail_child():
        raise root

    with pytest.raises(RuntimeError) as caught:
        await await_retained_originals(fail_child())
    assert caught.value is original
    assert original.__cause__ is cause
    assert original.__context__ is context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order", ["outer_then_child", "child_then_outer", "pending_outer", "already_done", "delivered_history", "repeated_outer"]
)
async def test_owned_result_notification_retains_exact_delivered_outer_and_child_objects(monkeypatch, order):
    original = asyncio.CancelledError("exact actual child")
    child_entered, child_release, owner_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    deliveries = []
    actual_shield = asyncio.shield

    def observe_shield(awaitable):
        async def observe():
            try:
                return await actual_shield(awaitable)
            except asyncio.CancelledError as delivery:
                deliveries.append(delivery)
                raise

        return observe()

    monkeypatch.setattr(asyncio, "shield", observe_shield)

    async def child():
        child_entered.set()
        await child_release.wait()
        raise original

    child_task = asyncio.create_task(child())
    await child_entered.wait()
    if order == "already_done":
        child_release.set()
        while not child_task.done():
            await asyncio.sleep(0)
    history = []

    async def owner():
        current = asyncio.current_task()
        assert current is not None
        if order == "delivered_history":
            current.cancel("already delivered history")
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError as previous:
                history.append(previous)
        if order == "pending_outer":
            current.cancel("pending at helper entry")
        owner_entered.set()
        return await await_retained_originals(child_task)

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    if order == "outer_then_child":
        owner_task.cancel("outer original")
        child_release.set()
    elif order == "child_then_outer":
        child_release.set()
        owner_task.cancel("outer original")
    elif order == "repeated_outer":
        for index in range(3):
            owner_task.cancel(f"outer original {index}")
            async with asyncio.timeout(5):
                while len(deliveries) != index + 1:
                    await asyncio.sleep(0)
            assert not owner_task.done()
        child_release.set()
    else:
        child_release.set()
    with pytest.raises(BaseException) as caught:
        await owner_task
    retained = leaves(caught.value)
    assert any(leaf is original for leaf in retained)
    assert all(any(leaf is delivery for leaf in retained) for delivery in deliveries)
    assert all(all(leaf is not previous for leaf in retained) for previous in history)
    expected = 3 if order == "repeated_outer" else 1 if order in ("outer_then_child", "child_then_outer", "pending_outer") else 0
    assert len(deliveries) == expected
    assert original.__cause__ is None and original.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["outer_then_child", "child_then_outer", "pending_outer", "delivered_history", "repeated_outer"])
async def test_actual_closed_lease_retains_child_and_outer_originals_without_synthetic_notifications(operation_store, monkeypatch, order):
    _engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    lease = await SessionOperationLease.acquire(
        repository,
        session_id=sid,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.MANUAL_PROPOSAL,
            lease.context,
            proposal_id=str(expected.authority.row.id),
            invocation_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    lease.bind_required_work(coordinator)
    actual_close = SessionOperationLease.close
    closed, release, owner_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = asyncio.CancelledError("actual child after physical close")

    async def finish_then_cancel(self):
        await actual_close(self)
        closed.set()
        await release.wait()
        raise original

    monkeypatch.setattr(SessionOperationLease, "close", finish_then_cancel)
    actual_shield = asyncio.shield
    deliveries = []

    def observe_shield(awaitable):
        async def observe():
            try:
                return await actual_shield(awaitable)
            except asyncio.CancelledError as delivery:
                deliveries.append(delivery)
                raise

        return observe()

    monkeypatch.setattr(asyncio, "shield", observe_shield)
    history = []

    async def owner():
        current = asyncio.current_task()
        assert current is not None
        if order == "delivered_history":
            current.cancel("historical close cancellation")
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError as previous:
                history.append(previous)
        if order == "pending_outer":
            current.cancel("pending close cancellation")
        owner_entered.set()
        return await close_required_proposal_lease(lease, coordinator=coordinator)

    task = asyncio.create_task(owner())
    await owner_entered.wait()
    await closed.wait()
    if order == "outer_then_child":
        task.cancel("outer close original")
        release.set()
    elif order == "child_then_outer":
        release.set()
        task.cancel("outer close original")
    elif order == "repeated_outer":
        for index in range(3):
            task.cancel(f"outer close original {index}")
            async with asyncio.timeout(5):
                while len(deliveries) != index + 1:
                    await asyncio.sleep(0)
            assert not task.done()
        release.set()
    else:
        release.set()
    errors = await task
    assert any(error is original for error in errors)
    assert all(any(error is delivery for error in errors) for delivery in deliveries)
    assert all(all(error is not previous for error in errors) for previous in history)
    assert lease.closed
    coordinator.assert_completed()
