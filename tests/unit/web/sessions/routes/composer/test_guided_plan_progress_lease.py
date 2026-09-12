"""Progress admission must not orphan separately owned guided authority."""

import asyncio
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.protocol import ComposerService
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import GuidedOperationFailed, GuidedOperationFence, SessionServiceProtocol
from elspeth.web.sessions.routes.composer import guided_plan
from elspeth.web.sessions.routes.guided_operations import GuidedOperationLease
from elspeth.web.sessions.schemas import GuidedPlanRequest


class _Authority:
    def __init__(self, context, events, close_failure):
        self.context = context
        self.events = events
        self.close_failure = close_failure

    def renew(self, context, *, lease_seconds):
        assert context == self.context
        return context

    def release(self, context):
        assert context == self.context
        self.events.append("close")
        if self.close_failure:
            raise RuntimeError("secondary close failure")


class _Limiter:
    async def check(self, user_id):
        assert user_id == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("secondary_failure", [False, True])
async def test_progress_claim_failure_terminalizes_then_closes_actual_lease(monkeypatch, cancelled, secondary_failure):
    session_id = uuid4()
    events = []
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=str(session_id), operation_id="session-operation", lease_token="lease", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    lease = SessionOperationLease(_Authority(context, events, secondary_failure), context, lease_seconds=30, renew_interval_seconds=10)
    reserved = GuidedOperationLease(
        fence=GuidedOperationFence(session_id=session_id, operation_id=str(uuid4()), lease_token="guided-lease", attempt=1),
        session_lease=lease,
    )
    service = create_autospec(SessionServiceProtocol, instance=True)
    composer = create_autospec(ComposerService, instance=True)
    app = FastAPI()
    app.state.session_service = service
    app.state.composer_service = composer
    request = Request({"type": "http", "method": "POST", "path": "/guided/plan", "headers": [], "query_string": b"", "app": app})
    limiter = _Limiter()
    monkeypatch.setattr(guided_plan, "_verify_session_ownership", create_autospec(guided_plan._verify_session_ownership))
    reserve = create_autospec(guided_plan.reserve_or_replay_guided_operation, return_value=reserved)
    monkeypatch.setattr(guided_plan, "reserve_or_replay_guided_operation", reserve)
    monkeypatch.setattr(guided_plan, "_get_composer_progress_registry", lambda _request: object())
    claim_started = asyncio.Event()
    settlement_started = asyncio.Event()
    allow_settlement = asyncio.Event()
    primary = RuntimeError("progress database claim failed")

    async def claim(*args, **kwargs):
        claim_started.set()
        if cancelled:
            await asyncio.Event().wait()
        raise primary

    async def fail(fence, **kwargs):
        assert fence == reserved.fence
        assert kwargs["session_operation_context"] == context
        assert kwargs["failure_code"] == ("request_cancelled" if cancelled else "operation_failed")
        events.append("terminal")
        settlement_started.set()
        await allow_settlement.wait()
        if secondary_failure:
            raise RuntimeError("secondary settlement failure")
        return GuidedOperationFailed(failure_code=kwargs["failure_code"])

    service.fail_guided_operation.side_effect = fail
    monkeypatch.setattr(guided_plan, "_composer_progress_sink", claim)
    task = asyncio.create_task(
        guided_plan.post_guided_plan(
            session_id,
            GuidedPlanRequest(operation_id=reserved.fence.operation_id, intent="Build a pipeline"),
            request,
            None,
            UserIdentity("user", "User"),
            limiter,
        )
    )
    try:
        await asyncio.wait_for(claim_started.wait(), timeout=2)
        if cancelled:
            task.cancel("original cancellation")
        await asyncio.wait_for(settlement_started.wait(), timeout=2)
        if cancelled:
            task.cancel("repeated cancellation during cleanup")
        allow_settlement.set()
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError) as caught:
            await task
        if cancelled:
            assert caught.value.args == ("original cancellation",)
        else:
            assert caught.value is primary
        assert events == ["terminal", "close"]
        assert lease.closed
        assert lease._renewal_task.done()
        service.fail_guided_operation.assert_awaited_once()
        composer.plan_guided_full_pipeline.assert_not_called()
    finally:
        allow_settlement.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not lease.closed:
            await lease.close()
