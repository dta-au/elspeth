from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import SessionOperationAuthority
from elspeth.web.sessions.routes import sessions


def test_fork_route_acquires_parent_before_guided_and_uses_composite_everywhere() -> None:
    source = inspect.getsource(sessions.register_session_routes)
    reserve = source.index("reserved = await reserve_or_replay_guided_operation")
    adopt = source.index("parent_lease = reserved.session_lease")
    assert reserve < adopt
    assert "session_operation_context=parent_operation_lease.context" in source
    assert "staged.authority," in source
    assert "fail_guided_fork_operation" in source
    # Merged custody shape: blob copy/cleanup run through HEAD's blob layer
    # under an exact BlobForkWriteFence derived from the guided fence, while
    # settlement and failure still consume the lane's composite authority
    # (open decision 1 on elspeth-4d6c0dd0f5 tracks re-deriving the lane's
    # authority-routed blob ledger).
    assert "cleanup_blobs_for_fork(" in source
    assert "BlobForkWriteFence(" in source


def test_fork_route_reverse_close_preserves_primary_retry_or_cancellation() -> None:
    source = inspect.getsource(sessions.register_session_routes)
    close_helper = inspect.getsource(sessions._close_fork_operation_leases)
    assert "for lease in (child, parent)" in close_helper
    assert "if primary is not None:" in close_helper
    assert "primary.add_note" in close_helper
    assert "sys.exception()" in source
    assert "SessionOperationFenceLost," in source


def _failing_lease(label: str, calls: list[str]) -> SessionOperationLease:
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=str(uuid4()), operation_id=str(uuid4()), lease_token="test-lease", operation_epoch=1),
        operation_kind=SessionOperationKind.SESSION_FORK,
    )
    authority = MagicMock(spec=SessionOperationAuthority)

    def fail_release(actual_context: SessionOperationContext) -> None:
        assert actual_context is context
        calls.append(label)
        raise SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)

    authority.release.side_effect = fail_release
    return SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary",
    [
        SessionOperationFenceLost(FenceLossReason.STALE_EPOCH),
        pytest.param(asyncio.CancelledError(), id="cancelled"),
    ],
)
async def test_reverse_close_never_replaces_stale_retry_or_cancellation(
    primary: BaseException,
) -> None:
    calls: list[str] = []
    child = _failing_lease("child", calls)
    parent = _failing_lease("parent", calls)

    with capture_logs() as logs:
        await sessions._close_fork_operation_leases(child, parent, primary)

    assert calls == ["child", "parent"]
    assert child.closed and parent.closed
    assert len(primary.__notes__) == 2
    records = [record for record in logs if record["event"] == "session.fork_lease_cleanup_failed"]
    assert [(record["session_id"], record["operation_id"], record["exc_class"]) for record in records] == [
        (lease.context.fence.session_id, lease.context.fence.operation_id, "SessionOperationFenceLost") for lease in (child, parent)
    ]
