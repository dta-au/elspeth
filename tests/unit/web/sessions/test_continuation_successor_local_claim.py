"""A real yielded successor cannot replace its still-finishing local predecessor."""

import asyncio
import threading
from uuid import uuid4

import httpx
import litellm
import pytest

from elspeth.web.sessions.composer_app_services import composer_app_services
from tests.helpers.composer_operations import message_body
from tests.unit.web.sessions.test_continuation_completion_owners import (
    _continuation_app,
    _effects,
    _end_admit,
    _LateToolSDK,
    _submit_and_settle,
)
from tests.unit.web.sessions.test_continuation_control_successor import _control, _segment_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("late_claim", [False, True])
async def test_real_successor_claim_defers_while_actual_predecessor_job_finishes(tmp_path, monkeypatch, late_claim):
    """Hold the real handoff return after commit, then run the actual claimant.

    The predecessor Task, release ticket, queued successor and SQL claims all
    come from production owners. No raw claim or placeholder job is inserted.
    """
    sdk = _LateToolSDK(
        [],
        [
            ("primary", ("Continuing this request.", [_control()])),
            ("primary", ("The same request continued.", None)),
            ("end", _end_admit()),
        ],
    )
    monkeypatch.setattr(litellm, "acompletion", sdk)
    defer_background_claims = threading.Event()
    yielded = threading.Event()
    release_handoff = threading.Event()
    returned_claims = []
    handed_back = []
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        worker = app.state.composer_async_worker
        authority = composer_app_services(app).composer_operation_authority
        assert authority is worker._authority
        repository = app.state.session_service.session_operation_authority
        claim_once = type(worker)._claim_once
        commit = type(repository).commit_checkpoint_and_successor
        claim_segment = type(authority).claim_segment
        release_claim = type(worker)._release_unstarted_claim

        async def background_claim(owner):
            if owner is worker and defer_background_claims.is_set():
                return 0
            return await claim_once(owner)

        def commit_then_hold(owner, running, **expectations):
            # Stop automatic scans before the successor becomes claimable;
            # the explicit actual claim below is the discriminating control.
            defer_background_claims.set()
            receipt = commit(owner, running, **expectations)
            yielded.set()
            if not release_handoff.wait(45.0):
                raise TimeoutError("test did not release the actual committed handoff return")
            return receipt

        def observe_claims(owner, **selection):
            assert type(selection["excluded_operations"]) is frozenset
            if late_claim and defer_background_claims.is_set():
                # Model a selection already issued before the local snapshot
                # changed. The returned claim is still actually acquired by
                # the real SQL authority, never a manufactured claim object.
                selection["excluded_operations"] = frozenset()
            claims = claim_segment(owner, **selection)
            returned_claims.extend(claims)
            return claims

        async def observe_handback(owner, claim):
            result = await release_claim(owner, claim)
            handed_back.append(claim)
            return result

        monkeypatch.setattr(type(worker), "_claim_once", background_claim)
        monkeypatch.setattr(type(repository), "commit_checkpoint_and_successor", commit_then_hold)
        monkeypatch.setattr(type(authority), "claim_segment", observe_claims)
        monkeypatch.setattr(type(worker), "_release_unstarted_claim", observe_handback)
        submission = asyncio.create_task(
            _submit_and_settle(app, token, session_id, message_body("Continue this request.", operation_id=operation_id))
        )
        try:
            assert await asyncio.to_thread(yielded.wait, 45.0), "actual atomic yield was not reached"
            key = (session_id, operation_id)
            predecessor = worker._jobs[key]
            cancellation = worker._local_cancels[key]
            assert not predecessor.done()
            before, successor = _segment_rows(app, operation_id)
            assert (before.sequence, before.state, successor.sequence, successor.state) == (1, "yielded", 2, "queued")
            assert successor.claim_token is None
            assert await claim_once(worker) == 0
            assert worker._jobs[key] is predecessor and worker._local_cancels[key] is cancellation
            assert not predecessor.done()
            successor_claims = tuple(claim for claim in returned_claims if claim.segment_sequence == 2)
            assert bool(successor_claims) is late_claim
            assert tuple(handed_back) == successor_claims
            assert _segment_rows(app, operation_id)[1].claim_token is None
            assert [request["role"] for request in sdk.requests] == ["primary"]
            assert {row.effect_key for row in _effects(app, operation_id)} == {"provider:0:0"}
        finally:
            # A RED on the old claimant can leave its real unstarted token.
            # Release that exact actual claim through the existing owner API.
            for claim in tuple(returned_claims):
                if claim.operation_id == operation_id and claim.segment_sequence == 2 and claim not in handed_back:
                    await worker._release_unstarted_claim(claim)
            release_handoff.set()
            defer_background_claims.clear()
            snapshot = await submission
        assert snapshot.status == "completed", snapshot.error
        assert [request["role"] for request in sdk.requests] == ["primary", "primary", "end"] and not sdk.steps
        assert tuple(row.sequence for row in _segment_rows(app, operation_id)) == (1, 2)


@pytest.mark.asyncio
async def test_local_exclusion_precedes_limit_and_does_not_hide_other_work_or_global_eligibility(tmp_path, monkeypatch):
    sdk = _LateToolSDK([], [])
    monkeypatch.setattr(litellm, "acompletion", sdk)
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        worker = app.state.composer_async_worker
        authority = composer_app_services(app).composer_operation_authority

        async def leave_jobs_queued(owner):
            return 0

        monkeypatch.setattr(type(worker), "_claim_once", leave_jobs_queued)
        sibling = await app.state.session_service.create_session(
            user_id="continuation-user", title="Other queued request", auth_provider_type="local"
        )
        requests = ((session_id, str(uuid4())), (sibling.id, str(uuid4())))
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
            for target, operation in requests:
                response = await client.post(
                    f"/api/sessions/{target}/messages", json=message_body("A queued request.", operation_id=operation)
                )
                assert response.status_code == 202, response.text
        # Measure the actual earliest eligible key rather than relying on
        # clock precision or UUID ordering to manufacture candidate order.
        (earliest,) = authority.claim_segment(limit=1)
        excluded = frozenset({(earliest.session_id, earliest.operation_id)})
        await worker._release_unstarted_claim(earliest)
        (other,) = authority.claim_segment(limit=1, excluded_operations=excluded)
        assert (other.session_id, other.operation_id) in frozenset(requests) - excluded
        await worker._release_unstarted_claim(other)
        # No persistent/global exclusion was written. The unfiltered actual
        # SQL API can still grant the exact previously blocked earliest key.
        (visible_without_local_hint,) = authority.claim_segment(limit=1)
        assert (visible_without_local_hint.session_id, visible_without_local_hint.operation_id) == (
            earliest.session_id,
            earliest.operation_id,
        )
        await worker._release_unstarted_claim(visible_without_local_hint)
        assert worker._jobs == {} and worker._local_cancels == {}
        assert sdk.requests == []
