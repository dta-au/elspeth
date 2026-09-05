"""``tests/helpers/session_fences.py`` mints EXECUTE leases that reach the production guard.

A test helper that hands code under test a lease the production guard cannot
see through is the fail-open-double class that reached staging twice
(elspeth-bb776978e3): the test passes, the guard never fires, and the defect
ships. Each test here therefore drives the REAL ``SessionOperationLease``
lifecycle (``lifecycle.py``) over the recording authority and proves that the
context the helper minted is the one the guard reproves, that a raising
compare-and-swap propagates out of ``guard_external_effect`` unchanged, and
that renewal keeps the helper lease alive rather than silently losing it.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from tests.helpers.session_fences import (
    RecordingSessionOperationAuthority,
    adopt_execute_lease,
    close_adopted_lease,
    execute_lease,
    make_blob_read_context,
    make_execute_context,
)

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease


def test_context_factories_mint_exact_contexts_bound_to_the_session() -> None:
    session_id = uuid4()
    execute_context = make_execute_context(session_id)
    blob_read_context = make_blob_read_context(str(session_id))

    assert type(execute_context) is SessionOperationContext
    assert execute_context.operation_kind is SessionOperationKind.EXECUTE
    assert execute_context.fence.session_id == str(session_id)
    assert type(blob_read_context) is SessionOperationContext
    assert blob_read_context.operation_kind is SessionOperationKind.BLOB_READ
    assert blob_read_context.fence.session_id == str(session_id)
    assert execute_context.fence != blob_read_context.fence


@pytest.mark.asyncio
async def test_a_raising_compare_and_swap_propagates_through_guard_external_effect() -> None:
    """The helper lease cannot no-op the guard: ``lifecycle.py:497-516`` runs the real CAS.

    First the guard is shown to reprove the exact helper-minted context through
    the authority; then the authority is made to lose the fence and the guard
    must surface that identical exception — not swallow it, not translate it.
    """
    session_id = uuid4()
    authority = RecordingSessionOperationAuthority()
    lost = SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)

    async with execute_lease(session_id, authority=authority) as lease:
        assert type(lease) is SessionOperationLease
        assert lease.context.operation_kind is SessionOperationKind.EXECUTE
        assert lease.fence.session_id == str(session_id)
        assert authority.calls == [("acquire", lease.context)]

        lease.guard_external_effect()
        assert authority.calls[-1] == ("compare_and_swap", lease.context)
        assert authority.calls[-1][1] is lease.context

        authority.compare_and_swap_error = lost
        with pytest.raises(SessionOperationFenceLost) as raised:
            lease.guard_external_effect()
        assert raised.value is lost

    assert lease.closed
    assert authority.calls[-1] == ("release", lease.context)
    assert authority.active == []


def test_adopt_execute_lease_binds_a_real_lease_to_the_given_loop() -> None:
    """The sync-test shape: adopt on an owned loop, guard from the test thread, close on the loop."""
    loop = asyncio.new_event_loop()
    try:
        session_id = uuid4()
        authority = RecordingSessionOperationAuthority()
        lease = adopt_execute_lease(loop, session_id, authority)

        assert type(lease) is SessionOperationLease
        assert lease.context.operation_kind is SessionOperationKind.EXECUTE
        assert lease.fence.session_id == str(session_id)
        assert authority.calls == [("compare_and_swap", lease.context)]

        lease.guard_external_effect()
        assert authority.calls[-1][1] is lease.context

        authority.compare_and_swap_error = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        with pytest.raises(SessionOperationFenceLost, match="token_mismatch"):
            lease.guard_external_effect()
        authority.compare_and_swap_error = None

        close_adopted_lease(loop, lease)
        assert lease.closed
        assert authority.calls[-1] == ("release", lease.context)
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_renewal_keeps_the_helper_lease_alive() -> None:
    """A recording authority whose ``renew`` drifted would lose every helper lease silently.

    The lifecycle records a loss when renewal returns anything but the identical
    context; this pins that the recording authority renews faithfully, and that
    a renewal failure it is told to raise is the one the lease reports.
    """
    session_id = uuid4()
    authority = RecordingSessionOperationAuthority()

    async with execute_lease(session_id, authority=authority, renew_interval_seconds=0.01) as lease:
        for _ in range(200):
            if any(name == "renew" for name, _ in authority.calls):
                break
            await asyncio.sleep(0.01)
        renewals = [context for name, context in authority.calls if name == "renew"]
        assert renewals, authority.calls
        assert all(context is lease.context for context in renewals)
        assert lease.renewal_error is None
        lease.guard_external_effect()

    renewal_lost = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    losing = RecordingSessionOperationAuthority()
    losing.renew_error = renewal_lost
    with pytest.raises(SessionOperationFenceLost) as raised:
        async with execute_lease(session_id, authority=losing, renew_interval_seconds=0.01) as lease:
            reported = await asyncio.wait_for(lease.wait_until_lost(), timeout=5)
            assert reported is renewal_lost
            with pytest.raises(SessionOperationFenceLost):
                lease.guard_external_effect()
    assert raised.value is renewal_lost
    assert not any(name == "release" for name, _ in losing.calls)
