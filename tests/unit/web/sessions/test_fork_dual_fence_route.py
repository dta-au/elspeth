from __future__ import annotations

import asyncio
import inspect

import pytest

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFenceLost,
)
from elspeth.web.sessions.routes import sessions


def test_fork_route_acquires_parent_before_guided_and_uses_composite_everywhere() -> None:
    source = inspect.getsource(sessions.register_session_routes)
    reserve = source.index("reserved = await reserve_or_replay_guided_operation")
    adopt = source.index("parent_lease = reserved.session_lease")
    assert reserve < adopt
    assert "session_operation_context=parent_operation_lease.context" in source
    assert "staged.authority," in source
    assert "fail_guided_fork_operation" in source
    assert "cleanup_blobs_for_fork(\n                            staged.authority" in source
    assert "BlobForkWriteFence(" not in source


def test_fork_route_reverse_close_preserves_primary_retry_or_cancellation() -> None:
    source = inspect.getsource(sessions.register_session_routes)
    close_helper = inspect.getsource(sessions._close_fork_operation_leases)
    assert "for lease in (child, parent)" in close_helper
    assert "if primary is not None:" in close_helper
    assert "primary.add_note" in close_helper
    assert "sys.exception()" in source
    assert "SessionOperationFenceLost," in source


class _FailingLease:
    def __init__(self, label: str, calls: list[str]) -> None:
        self.closed = False
        self._label = label
        self._calls = calls

    async def close(self) -> None:
        self._calls.append(self._label)
        raise SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)


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
    child = _FailingLease("child", calls)
    parent = _FailingLease("parent", calls)

    await sessions._close_fork_operation_leases(  # type: ignore[arg-type]
        child,
        parent,
        primary,
    )

    assert calls == ["child", "parent"]
    assert len(primary.__notes__) == 2
