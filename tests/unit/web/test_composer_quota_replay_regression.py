"""Behavioral P2 control shared unchanged by baseline and candidate epochs."""

import asyncio
from threading import Event
from uuid import uuid4

import httpx
import pytest

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from tests.helpers.composer_operations import build_composer_operation_app


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["messages", "recompose"])
async def test_same_http_action_replay_has_one_slot_even_before_first_insert(tmp_path, monkeypatch, endpoint):
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app
    app.state.rate_limiter._limit = 1
    entered, release, exited = Event(), Event(), Event()
    actual = ComposerAsyncOperationAuthority.admit

    def hold_first(self, **kwargs):
        first = not entered.is_set()
        if first:
            entered.set()
            assert release.wait(5), "admission seam was not released"
        try:
            return actual(self, **kwargs)
        finally:
            if first:
                exited.set()

    monkeypatch.setattr(ComposerAsyncOperationAuthority, "admit", hold_first)
    body = {"operation_id": str(uuid4())}
    body.update({"content": "Build"} if endpoint == "messages" else {"expected_user_message_id": str(uuid4())})
    path = f"/api/sessions/{fixture.session_id}/{endpoint}"
    first = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {fixture.token}"}
        ) as client:
            first = asyncio.create_task(client.post(path, json=body))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                duplicate = await client.post(path, json=body)
            finally:
                release.set()
                accepted = await first
            assert exited.is_set()
            assert accepted.status_code == 202
            # Baseline P2 fails here with 429 although the immutable action is
            # identical. The candidate admits once and returns 202 twice.
            assert duplicate.status_code == 202, duplicate.text
            assert accepted.json() == duplicate.json()
    finally:
        release.set()
        if first is not None and not first.done():
            await first
        app.state.session_engine.dispose()
