"""App-owned progress heartbeat custody without an HTTP request."""

import asyncio
import time

import pytest
from starlette.exceptions import HTTPException

from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost
from elspeth.web.sessions.routes._helpers import _ComposerHeartbeatTimer, composer_request_lifecycle


class _LostRegistry(ComposerProgressRegistry):
    async def renew_request(self, lease):
        raise ComposerRequestLeaseLost("Exact test lease lost")


@pytest.mark.asyncio
async def test_app_lifecycle_renewal_loss_cleans_exact_lease() -> None:
    registry = _LostRegistry()

    async def wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    owner = asyncio.current_task()
    assert owner is not None
    with pytest.raises(HTTPException) as failure:
        async with composer_request_lifecycle(
            registry, session_id="session", user_id="alice", owner_task=owner, timer=_ComposerHeartbeatTimer(now=time.monotonic, wait=wait)
        ):
            await asyncio.sleep(10)
    assert failure.value.status_code == 503
    assert (await registry.get_latest("session")).inflight_requests == 0
    assert owner.cancelling() == 0


@pytest.mark.asyncio
async def test_app_lifecycle_keeps_work_until_its_own_exit() -> None:
    registry = ComposerProgressRegistry()
    owner = asyncio.current_task()
    assert owner is not None
    async with composer_request_lifecycle(registry, session_id="session", user_id="alice", owner_task=owner) as lifecycle:
        assert lifecycle.lease.session_id == "session"
        assert (await registry.get_latest("session")).inflight_requests == 1
        lifecycle.durable_completed = True
    assert (await registry.get_latest("session")).inflight_requests == 0
