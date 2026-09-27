"""Route guard accepts an already-settled mode-neutral receipt."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from elspeth.web.sessions.protocol import OperationReceiptFence, OperationReceiptFenceLostError
from elspeth.web.sessions.routes.operation_receipts import OperationReceiptLease, operation_receipt_lease_guard


@pytest.mark.asyncio
async def test_lease_guard_accepts_already_terminal_receipt_on_normal_return() -> None:
    fence = OperationReceiptFence(session_id=uuid4(), operation_id="fork-1", lease_token="token", attempt=1)
    service = SimpleNamespace(fail_operation_receipt=AsyncMock(side_effect=OperationReceiptFenceLostError(fence)))
    session_lease = SimpleNamespace(context=object(), close=AsyncMock())
    guard = operation_receipt_lease_guard(service=service, lease=OperationReceiptLease(fence=fence, session_lease=session_lease))
    await guard.finish_active_exception()
    service.fail_operation_receipt.assert_awaited_once()
    session_lease.close.assert_awaited_once()
