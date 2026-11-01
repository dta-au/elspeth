"""Actual action expiry interrupts a worker stage without abandoning SQL custody."""

import asyncio
import json
from threading import Event
from uuid import uuid4

import httpx
import litellm
import pytest
from litellm import ModelResponse

from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operation_errors import deadline_expired_error
from elspeth.web.sessions.composer_operations import ComposerOperationRecord
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.schemas import ComposerOperationSnapshot
from tests.helpers.composer_operations import message_body
from tests.unit.web.sessions.test_continuation_completion_owners import _continuation_app, _effects, _end_admit


async def _wait(predicate, *, message, seconds=3):
    try:
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(message) from None


def _short_original_primary_clock(monkeypatch, operation_id):
    """Admit the real policy with most of its original time already spent.

    Only this test's first PRIMARY allowance is backdated. Production's one
    actual begin_action call still installs the exact retained policy; no
    later read, signal, settlement or retry may move the recorded expiry.
    """
    original = ContinuationSQLDeadlineOwner.begin_action
    observed = {}

    def begin(owner, action, *, started_at):
        if owner.running.claim.operation_id == operation_id and action == owner.policy.actions.primary and not observed:
            original(owner, action, started_at=started_at - action.total + 0.25)
            observed.update(owner=owner, expires_at=owner._action_expires_at, action=action)
        else:
            original(owner, action, started_at=started_at)

    monkeypatch.setattr(ContinuationSQLDeadlineOwner, "begin_action", begin)
    return observed


def _scripted_external_provider(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs["model"])
        content = "The current pipeline has no transformations." if kwargs["model"] == "test/model" else json.dumps(_end_admit())
        return ModelResponse(
            model=kwargs["model"],
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": content, "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    return calls


def _pending_ticket(owner, source):
    pending = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is source and not ticket.complete)
    assert len(pending) == 1
    return pending[0]


async def _snapshot(client, session_id, operation_id):
    response = await client.get(f"/api/sessions/{session_id}/operations/{operation_id}")
    assert response.status_code == 200, response.text
    return ComposerOperationSnapshot.model_validate_json(response.content, strict=True)


async def _settled(client, app, session_id, operation_id):
    snapshot = None
    async with asyncio.timeout(30):
        while True:
            snapshot = await _snapshot(client, session_id, operation_id)
            if snapshot.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)
        await app.state.composer_async_worker.run_until_idle()
    return snapshot


async def _assert_expiry_joins_pending_stage(probe, owner, ticket):
    await _wait(
        lambda: probe["stage"].cancelling() > 0,
        message="Original action expiry did not cancel the actual lease-owned stage",
    )
    assert any(item.key.source is RequiredWorkSource.DEADLINE_SIGNAL for item in owner.work.coordinator.tickets)
    assert not probe["stage"].done() and not ticket.complete
    assert ticket._future is not None and ticket._future.running() and not ticket._future.cancelled()
    key = (owner.running.claim.session_id, owner.running.claim.operation_id)
    assert key in owner.worker._jobs and not owner.worker._jobs[key].done()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["deadline", "durable-stop-after-expiry"])
async def test_original_action_expiry_cancels_actual_stage_but_joins_held_read(tmp_path, monkeypatch, stop):
    operation_id = str(uuid4())
    clock = _short_original_primary_clock(monkeypatch, operation_id)
    calls = _scripted_external_provider(monkeypatch)
    entered, release = Event(), Event()
    probe = {}
    actual_advance = _SegmentDriver.advance

    async def advance(owner, stored, selected):
        top = selected.stack.frames[-1]
        if owner.running.claim.operation_id != operation_id or type(top) is not ComposeFrame or top.phase != "action_ready" or probe:
            return await actual_advance(owner, stored, selected)
        probe.update(owner=owner, stage=asyncio.current_task())

        def read():
            actual = owner.authority.load_continuation_snapshot(owner.running)
            entered.set()
            assert release.wait(10)
            probe["physical_returned"] = True
            return actual

        await _registered(
            work=owner.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=read,
            expected=StoredContinuationSnapshot,
        )
        probe["advanced_after_read"] = True
        return await actual_advance(owner, stored, selected)

    monkeypatch.setattr(_SegmentDriver, "advance", advance)
    async with (
        _continuation_app(tmp_path, monkeypatch) as (app, token, session_id),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
        ) as client,
    ):
        try:
            accepted = await client.post(
                f"/api/sessions/{session_id}/messages", json=message_body("Explain this pipeline.", operation_id=operation_id)
            )
            assert accepted.status_code == 202, accepted.text
            await _wait(entered.is_set, message="Actual registered read was not entered", seconds=30)
            owner = probe["owner"]
            ticket = _pending_ticket(owner, RequiredWorkSource.CONTINUATION_READ_SQL)
            await _assert_expiry_joins_pending_stage(probe, owner, ticket)
            assert clock["owner"] is owner.sql_deadline and owner.sql_deadline._action_expires_at == clock["expires_at"]
            assert (await _snapshot(client, session_id, operation_id)).status == "running"
            if stop:
                owner.authority.request_cancel(session_id=session_id, operation_id=operation_id)
            assert calls == [] and not probe.get("advanced_after_read", False)
            release.set()
            snapshot = await _settled(client, app, session_id, operation_id)
            assert snapshot.status == "failed" and snapshot.error is not None
            assert snapshot.error.failure_code == ("request_cancelled" if stop else "deadline_expired")
            assert probe["physical_returned"] and not probe.get("advanced_after_read", False)
            assert ticket.complete and type(ticket._finish_once_handoff) is RequiredSQLReturned
            assert calls == [] and _effects(app, operation_id) == ()
            assert owner.sql_deadline._action_expires_at == clock["expires_at"]
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [True, False], ids=["sealed-terminal-commit", "actual-terminal-rollback"])
async def test_action_expiry_joins_actual_terminal_commit_or_rollback_without_guessing(tmp_path, monkeypatch, commit):
    from elspeth.web.coordination import composer_operation_authority as sql_owner

    operation_id = str(uuid4())
    clock = _short_original_primary_clock(monkeypatch, operation_id)
    calls = _scripted_external_provider(monkeypatch)
    entered, release = Event(), Event()
    probe = {}
    rollback = RuntimeError("original controlled terminal transaction rollback")
    actual_advance = _SegmentDriver.advance
    actual_terminal = sql_owner.settle_composer_operation_on_connection

    def terminal(conn, running, **kwargs):
        candidate = actual_terminal(conn, running, **kwargs)
        if running.claim.operation_id == operation_id:
            probe.setdefault("terminal_candidates", []).append(candidate)
        if running.claim.operation_id == operation_id and not entered.is_set():
            probe["candidate"] = candidate
            entered.set()
            assert release.wait(10)
            if not commit:
                raise rollback
        return candidate

    async def advance(owner, stored, selected):
        top = selected.stack.frames[-1]
        if owner.running.claim.operation_id != operation_id or type(top) is not ComposeFrame or top.phase != "action_ready" or probe:
            return await actual_advance(owner, stored, selected)
        probe.update(owner=owner, stage=asyncio.current_task())
        # A real terminal SQL owner stages its normal deadline failure before
        # commit. While held it is unknown, and only its actual transaction
        # exit determines whether this candidate became authoritative.
        return await _registered(
            work=owner.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: owner.authority.settle_continuation_owner(
                owner.running,
                outcome=deadline_expired_error(request_id=None, timeout_seconds=clock["action"].total),
            ),
            expected=ComposerOperationRecord,
        )

    monkeypatch.setattr(sql_owner, "settle_composer_operation_on_connection", terminal)
    monkeypatch.setattr(_SegmentDriver, "advance", advance)
    async with (
        _continuation_app(tmp_path, monkeypatch) as (app, token, session_id),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
        ) as client,
    ):
        try:
            accepted = await client.post(
                f"/api/sessions/{session_id}/messages", json=message_body("Explain this pipeline.", operation_id=operation_id)
            )
            assert accepted.status_code == 202, accepted.text
            await _wait(entered.is_set, message="Actual terminal transaction was not entered", seconds=30)
            owner = probe["owner"]
            ticket = _pending_ticket(owner, RequiredWorkSource.CONTINUATION_STATE_SQL)
            await _assert_expiry_joins_pending_stage(probe, owner, ticket)
            assert (await _snapshot(client, session_id, operation_id)).status == "running"
            assert calls == [] and probe["candidate"].status == "failed"
            release.set()
            snapshot = await _settled(client, app, session_id, operation_id)
            assert snapshot.status == "failed" and snapshot.error is not None
            assert snapshot.error.failure_code == "deadline_expired"
            assert ticket.complete and ticket._future is not None and not ticket._future.cancelled()
            physical = ticket._finish_once_handoff
            if commit:
                assert type(physical) is RequiredSQLReturned and physical.value is probe["candidate"]
                assert snapshot.status_revision == probe["candidate"].status_revision
                assert len(probe["terminal_candidates"]) == 1
            else:
                assert type(physical) is RequiredSQLRaised and physical.error is rollback
                # The rolled-back candidate is not a sealed result. The
                # current owner must make and verify its later terminal.
                assert len(probe["terminal_candidates"]) == 2
                assert snapshot.status_revision == probe["terminal_candidates"][-1].status_revision
            assert calls == [] and _effects(app, operation_id) == ()
            assert owner.sql_deadline._action_expires_at == clock["expires_at"]
        finally:
            release.set()
